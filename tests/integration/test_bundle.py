#!/usr/bin/env python3

#  Copyright 2021 Canonical Ltd.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import inspect
import json
import logging
import os
import urllib.request
from pathlib import Path

import pytest
from helpers import (
    cli_deploy_bundle,
    get_alertmanager_alerts,
    get_alertmanager_groups,
    get_unit_address,
)

log = logging.getLogger(__name__)
juju_topology_keys = {"juju_model_uuid", "juju_model", "juju_application"}


def get_this_script_dir() -> Path:
    filename = inspect.getframeinfo(inspect.currentframe()).filename  # type: ignore[arg-type]
    path = os.path.dirname(os.path.abspath(filename))
    return Path(path)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test, pytestconfig):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    log.info("Rendering bundle %s", get_this_script_dir() / ".." / ".." / "bundle.yaml.j2")

    async def build_charm_if_is_dir(option: str) -> str:
        if Path(option).is_dir():
            logging.info("Building charm from source: %s", option)
            option = await ops_test.build_charm(option)
        return str(option)

    charms = dict(
        alertmanager=pytestconfig.getoption("alertmanager"),
        prometheus=pytestconfig.getoption("prometheus"),
        grafana=pytestconfig.getoption("grafana"),
        loki=pytestconfig.getoption("loki"),
        avalanche=pytestconfig.getoption("avalanche"),
    )

    additional_args = dict(
        channel=pytestconfig.getoption("channel"),
    )

    context = {k: await build_charm_if_is_dir(v) for k, v in charms.items() if v is not None}
    context.update(additional_args)

    # set the "testing" template variable so the template renders for testing
    context["testing"] = "true"

    logging.debug("context: %s", context)

    rendered_bundle = ops_test.render_bundle(
        get_this_script_dir() / ".." / ".." / "bundle.yaml.j2", context=context
    )

    # use CLI to deploy bundle until https://github.com/juju/python-libjuju/issues/511 is fixed.
    await cli_deploy_bundle(ops_test, str(rendered_bundle))

    # due to a juju bug, occasionally some charms finish a startup sequence with "waiting for IP
    # address"
    # issuing dummy update_status just to trigger an event
    await ops_test.model.set_config({"update-status-hook-interval": "10s"})

    await ops_test.model.wait_for_idle(status="active", timeout=1000)
    assert ops_test.model.applications["alertmanager"].units[0].workload_status == "active"

    # effectively disable the update status from firing
    await ops_test.model.set_config({"update-status-hook-interval": "60m"})


@pytest.mark.abort_on_fail
async def test_alertmanager_is_up(ops_test):
    address = await get_unit_address(ops_test, "alertmanager", 0)
    url = f"http://{address}:9093"
    log.info("am public address: %s", url)

    response = urllib.request.urlopen(f"{url}/api/v2/status", data=None, timeout=2.0)
    assert response.code == 200
    assert "versionInfo" in json.loads(response.read())


@pytest.mark.abort_on_fail
async def test_prometheus_is_up(ops_test):
    address = await get_unit_address(ops_test, "prometheus", 0)
    url = f"http://{address}:9090"
    log.info("prom public address: %s", url)

    response = urllib.request.urlopen(f"{url}/-/ready", data=None, timeout=2.0)
    assert response.code == 200


@pytest.mark.abort_on_fail
async def test_prometheus_sees_alertmanager(ops_test):
    am_address = await get_unit_address(ops_test, "alertmanager", 0)
    prom_address = await get_unit_address(ops_test, "prometheus", 0)

    response = urllib.request.urlopen(
        f"http://{prom_address}:9090/api/v1/alertmanagers", data=None, timeout=2.0
    )
    assert response.code == 200
    alertmanagers = json.loads(response.read())
    # an empty response looks like this:
    # {"status":"success","data":{"activeAlertmanagers":[],"droppedAlertmanagers":[]}}
    # a jsonified activeAlertmanagers looks like this:
    # [{'url': 'http://10.1.179.124:9093/api/v1/alerts'}]
    assert any(
        f"http://{am_address}:9093" in am["url"]
        for am in alertmanagers["data"]["activeAlertmanagers"]
    )


async def test_juju_topology_labels_in_alerts(ops_test):
    alerts = await get_alertmanager_alerts(ops_test, "alertmanager", 0, retries=100)

    i = -1
    for i, alert in enumerate(alerts):
        # make sure every alert has all the juju topology labels
        # NOTE this would only test alerts that are already firing while testing
        assert alert["labels"].keys() >= juju_topology_keys

        # make sure the juju topology entries are not empty
        assert all(alert["labels"][key] for key in juju_topology_keys)

    assert i >= 0  # should have at least one alarms listed (the "AlwaysFiring" alarm rule)
    log.info("juju topology test passed for %s alerts", i + 1)


async def test_alerts_are_grouped(ops_test):
    groups = await get_alertmanager_groups(ops_test, "alertmanager", 0, retries=100)
    i = -1
    for i, group in enumerate(groups):
        # make sure all groups are grouped by juju topology keys
        assert group["labels"].keys() == juju_topology_keys

    assert i >= 0  # should have at least one group listed (the "AlwaysFiring" alarm rule)
    log.info("juju topology grouping test passed for %s groups", i + 1)