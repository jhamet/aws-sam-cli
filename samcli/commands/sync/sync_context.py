"""
Context object used by sync command
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, cast

import tomlkit
from tomlkit.items import Item
from tomlkit.toml_document import TOMLDocument

from samcli.lib.build.build_graph import DEFAULT_DEPENDENCIES_DIR
from samcli.lib.utils.osutils import rmtree_if_exists

LOG = logging.getLogger(__name__)


DEFAULT_SYNC_STATE_FILE_NAME = "sync.toml"

SYNC_STATE = "sync_state"
RESOURCE_SYNC_STATES = "resource_sync_states"
HASH = "hash"
SYNC_TIME = "sync_time"
DEPENDENCY_LAYER = "dependency_layer"

# global lock for writing to file
_lock = threading.Lock()


@dataclass
class ResourceSyncState:
    hash_value: str
    sync_time: datetime


@dataclass
class SyncState:
    dependency_layer: bool
    resource_sync_states: Dict[str, ResourceSyncState]

    def update_resource_sync_state(self, resource_id: str, hash_value: str):
        self.resource_sync_states[resource_id] = ResourceSyncState(hash_value, datetime.utcnow())


def _sync_state_to_toml_document(sync_state: SyncState) -> TOMLDocument:
    """
    Writes the sync state information to the TOML file.

    Parameters
    -------
    sync_state: SyncState
        The SyncState to cache the information in the TOML file

    Returns
    -------
    TOMLDocument
        Object which will be dumped to the TOML file
    """
    sync_state_toml_table = tomlkit.table()
    sync_state_toml_table[DEPENDENCY_LAYER] = sync_state.dependency_layer

    resource_sync_states_toml_table = tomlkit.table()
    for resource_id in sync_state.resource_sync_states:
        resource_sync_state = sync_state.resource_sync_states[resource_id]

        resource_sync_state_toml_table = tomlkit.table()

        resource_sync_state_toml_table[HASH] = resource_sync_state.hash_value
        resource_sync_state_toml_table[SYNC_TIME] = resource_sync_state.sync_time.isoformat()

        # For Nested stack resources, replace "/" with "-"
        resource_id_toml = resource_id.replace("/", "-")
        resource_sync_states_toml_table[resource_id_toml] = resource_sync_state_toml_table

    toml_document = tomlkit.document()
    toml_document.add((tomlkit.comment("This file is auto generated by SAM CLI sync command")))
    toml_document.add(SYNC_STATE, cast(Item, sync_state_toml_table))
    toml_document.add(RESOURCE_SYNC_STATES, cast(Item, resource_sync_states_toml_table))

    return toml_document


def _toml_document_to_sync_state(toml_document: Dict) -> Optional[SyncState]:
    """
    Reads the cached information from the provided toml_document.

    Parameters
    -------
    toml_document: SyncState
        The toml document to read the information from

    """
    if not toml_document:
        return None

    sync_state_toml_table = toml_document.get(SYNC_STATE)
    resource_sync_states_toml_table = toml_document.get(RESOURCE_SYNC_STATES, {})

    # If no info in toml file
    if not (sync_state_toml_table or resource_sync_states_toml_table):
        return None

    resource_sync_states = dict()
    if resource_sync_states_toml_table:
        for resource_id in resource_sync_states_toml_table:
            resource_sync_state_toml_table = resource_sync_states_toml_table.get(resource_id)
            resource_sync_state = ResourceSyncState(
                resource_sync_state_toml_table.get(HASH),
                datetime.fromisoformat(resource_sync_state_toml_table.get(SYNC_TIME)),
            )

            # For Nested stack resources, replace "-" with "/"
            resource_sync_state_resource_id = resource_id.replace("-", "/")
            resource_sync_states[resource_sync_state_resource_id] = resource_sync_state

    dependency_layer = False
    if sync_state_toml_table:
        dependency_layer = sync_state_toml_table.get(DEPENDENCY_LAYER)
    sync_state = SyncState(dependency_layer, resource_sync_states)

    return sync_state


class SyncContext:
    _current_state: SyncState
    _previous_state: Optional[SyncState]
    _build_dir: Path
    _cache_dir: Path
    _file_path: Path

    def __init__(self, dependency_layer: bool, build_dir: str, cache_dir: str):
        self._current_state = SyncState(dependency_layer, dict())
        self._previous_state = None
        self._build_dir = Path(build_dir)
        self._cache_dir = Path(cache_dir)
        self._file_path = Path(build_dir).parent.joinpath(DEFAULT_SYNC_STATE_FILE_NAME)

    def __enter__(self) -> "SyncContext":
        with _lock:
            self._read()
        LOG.debug(
            "Entering sync context, previous state: %s, current state: %s", self._previous_state, self._current_state
        )

        # if adl parameter is changed between sam sync runs, cleanup build, cache and dependencies folders
        if self._previous_state and self._previous_state.dependency_layer != self._current_state.dependency_layer:
            self._cleanup_build_folders()

        return self

    def __exit__(self, *args) -> None:
        with _lock:
            self._write()

    def update_resource_sync_state(self, resource_id: str, hash_value: str) -> None:
        """
        Updates the sync_state information for the provided resource_id
        to be stored in the TOML file.

        Parameters
        -------
        resource_id: str
            The resource identifier of the resource
        hash_value: str
            The logical ID identifier of the resource
        """
        with _lock:
            LOG.debug("Updating resource_sync_state for resource %s with hash %s", resource_id, hash_value)
            self._current_state.update_resource_sync_state(resource_id, hash_value)
            self._write()

    def get_resource_latest_sync_hash(self, resource_id: str) -> Optional[str]:
        """
        Returns the latest hash from resource_sync_state if this information was
        cached for the provided resource_id.

        Parameters
        -------
        resource_id: str
            The resource identifier of the resource

        Returns
        -------
        Optional[str]
            The hash of the resource stored in resource_sync_state if it exists
        """
        with _lock:
            resource_sync_state = self._current_state.resource_sync_states.get(resource_id)
            if not resource_sync_state:
                LOG.debug("No latest hash found for resource %s", resource_id)
                return None
            LOG.debug(
                "Latest resource_sync_state hash %s found for resource %s", resource_id, resource_sync_state.hash_value
            )
            return resource_sync_state.hash_value

    def _write(self) -> None:
        with open(self._file_path, "w+") as file:
            file.write(tomlkit.dumps(_sync_state_to_toml_document(self._current_state)))

    def _read(self) -> None:
        try:
            with open(self._file_path) as file:
                toml_document = cast(Dict, tomlkit.loads(file.read()))
            self._previous_state = _toml_document_to_sync_state(toml_document)
            if self._previous_state:
                self._current_state.resource_sync_states = self._previous_state.resource_sync_states
        except OSError:
            LOG.debug("Missing previous sync state, will create a new file at the end of this execution")

    def _cleanup_build_folders(self) -> None:
        """
        Cleans up build, cache and dependencies folders for clean start of the next session
        """
        LOG.debug("Cleaning up build directory %s", self._build_dir)
        rmtree_if_exists(self._build_dir)

        LOG.debug("Cleaning up cache directory %s", self._cache_dir)
        rmtree_if_exists(self._cache_dir)

        dependencies_dir = Path(DEFAULT_DEPENDENCIES_DIR)
        LOG.debug("Cleaning up dependencies directory: %s", dependencies_dir)
        rmtree_if_exists(dependencies_dir)
