# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import json
import logging
from typing import Any

from flask import g
from sqlalchemy.orm import Session

from superset import security_manager
from superset.commands.exceptions import ImportFailedError
from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)


JSON_KEYS = {"position": "position_json", "metadata": "json_metadata"}


def find_chart_uuids(position: dict[str, Any]) -> set[str]:
    return set(build_uuid_to_id_map(position))


def find_native_filter_datasets(metadata: dict[str, Any]) -> set[str]:
    uuids: set[str] = set()
    for native_filter in metadata.get("native_filter_configuration", []):
        targets = native_filter.get("targets", [])
        for target in targets:
            dataset_uuid = target.get("datasetUuid")
            if dataset_uuid:
                uuids.add(dataset_uuid)
    return uuids


def build_uuid_to_id_map(position: dict[str, Any]) -> dict[str, int]:
    return {
        child["meta"]["uuid"]: child["meta"]["chartId"]
        for child in position.values()
        if (
            isinstance(child, dict)
            and child["type"] == "CHART"
            and "uuid" in child["meta"]
        )
    }


def update_id_refs(  # pylint: disable=too-many-locals
    config: dict[str, Any],
    chart_ids: dict[str, int],
    dataset_info: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Update dashboard metadata to use new IDs"""
    fixed = config.copy()

    # build map old_id => new_id
    old_ids = build_uuid_to_id_map(fixed["position"])
    id_map = {
        old_id: chart_ids[uuid] for uuid, old_id in old_ids.items() if uuid in chart_ids
    }

    # fix metadata
    metadata = fixed.get("metadata", {})
    if "timed_refresh_immune_slices" in metadata:
        metadata["timed_refresh_immune_slices"] = [
            id_map[old_id] for old_id in metadata["timed_refresh_immune_slices"]
        ]

    if "filter_scopes" in metadata:
        # in filter_scopes the key is the chart ID as a string; we need to update
        # them to be the new ID as a string:
        metadata["filter_scopes"] = {
            str(id_map[int(old_id)]): columns
            for old_id, columns in metadata["filter_scopes"].items()
            if int(old_id) in id_map
        }

        # now update columns to use new IDs:
        for columns in metadata["filter_scopes"].values():
            for attributes in columns.values():
                attributes["immune"] = [
                    id_map[old_id]
                    for old_id in attributes["immune"]
                    if old_id in id_map
                ]

    if "expanded_slices" in metadata:
        metadata["expanded_slices"] = {
            str(id_map[int(old_id)]): value
            for old_id, value in metadata["expanded_slices"].items()
        }

    if "default_filters" in metadata:
        default_filters = json.loads(metadata["default_filters"])
        metadata["default_filters"] = json.dumps(
            {
                str(id_map[int(old_id)]): value
                for old_id, value in default_filters.items()
                if int(old_id) in id_map
            }
        )

    # fix position
    position = fixed.get("position", {})
    for child in position.values():
        if (
            isinstance(child, dict)
            and child["type"] == "CHART"
            and "uuid" in child["meta"]
            and child["meta"]["uuid"] in chart_ids
        ):
            child["meta"]["chartId"] = chart_ids[child["meta"]["uuid"]]

    # fix native filter references
    native_filter_configuration = fixed.get("metadata", {}).get(
        "native_filter_configuration", []
    )
    for native_filter in native_filter_configuration:
        targets = native_filter.get("targets", [])
        for target in targets:
            dataset_uuid = target.pop("datasetUuid", None)
            if dataset_uuid:
                target["datasetId"] = dataset_info[dataset_uuid]["datasource_id"]

        scope_excluded = native_filter.get("scope", {}).get("excluded", [])
        if scope_excluded:
            native_filter["scope"]["excluded"] = [
                id_map[old_id] for old_id in scope_excluded if old_id in id_map
            ]
    fixed=update_cross_filter_scoping(fixed,id_map)
    return fixed

def update_cross_filter_scoping(config: dict[str, Any],
    id_map: dict[int, int]
    ) -> dict[str, Any]:
    # fix cross filter references
    cross_filter_global_config = config.get("metadata", {}).get(
        "global_chart_configuration", {}
    )
    scope_excluded = cross_filter_global_config.get("scope", {}).get("excluded", [])
    if scope_excluded:
        cross_filter_global_config["scope"]["excluded"] = [
            id_map[old_id] for old_id in scope_excluded if old_id in id_map
        ]
    metadata = config.get("metadata", {})
    if "chart_configuration" in metadata:
        # in cross_filter_scopes the key is the chart ID as a string; we need to update
        # them to be the new ID as a string:
        metadata["chart_configuration"] = {
            str(id_map[int(old_id)]): columns
            for old_id, columns in metadata["chart_configuration"].items()
            if int(old_id) in id_map
        }
        # now update scope excluded to use new IDs:
        for excluded_charts in metadata["chart_configuration"].values():
            if excluded_charts["id"] in id_map:
                excluded_charts["id"]=id_map[excluded_charts["id"]]
            if isinstance(excluded_charts["crossFilters"]["scope"],dict):
                scope_excluded=excluded_charts["crossFilters"]["scope"]["excluded"]
                if scope_excluded:
                    excluded_charts["crossFilters"]["scope"]["excluded"] = [
                    id_map[old_id] for old_id in scope_excluded if old_id in id_map
                ]
    return config
    
def import_dashboard(
    session: Session,
    config: dict[str, Any],
    overwrite: bool = False,
    ignore_permissions: bool = False,
) -> Dashboard:
    can_write = ignore_permissions or security_manager.can_access(
        "can_write",
        "Dashboard",
    )
    existing = session.query(Dashboard).filter_by(uuid=config["uuid"]).first()
    if existing:
        if not overwrite or not can_write:
            return existing
        config["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Dashboard doesn't exist and user doesn't "
            "have permission to create dashboards"
        )

    # TODO (betodealmeida): move this logic to import_from_dict
    config = config.copy()

    # removed in https://github.com/apache/superset/pull/23228
    if "metadata" in config and "show_native_filters" in config["metadata"]:
        del config["metadata"]["show_native_filters"]

    for key, new_name in JSON_KEYS.items():
        if config.get(key) is not None:
            value = config.pop(key)
            try:
                config[new_name] = json.dumps(value)
            except TypeError:
                logger.info("Unable to encode `%s` field: %s", key, value)

    dashboard = Dashboard.import_from_dict(session, config, recursive=False)
    if dashboard.id is None:
        session.flush()

    if hasattr(g, "user") and g.user:
        dashboard.owners.append(g.user)

    return dashboard
