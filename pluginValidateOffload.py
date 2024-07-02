import json
import typing
from typing import Optional

import common
import perf
import pluginbase
import tftbase

from logger import logger
from task import PluginTask
from task import TaskOperation
from testSettings import TestSettings
from tftbase import BaseOutput
from tftbase import PluginOutput
from tftbase import PodType


VF_REP_TRAFFIC_THRESHOLD = 1000


def ethtool_stat_parse(output: str) -> dict[str, str]:
    result = {}
    for line in output.splitlines():
        try:
            key, val = line.split(":", 2)
        except Exception:
            continue
        if val == "" and " " in key:
            # This is a section heading.
            continue
        result[key.strip()] = val.strip()
    return result


def ethtool_stat_get_packets(data: dict[str, str], packet_type: str) -> Optional[int]:

    # Case1: Try to parse rx_packets and tx_packets from ethtool output
    val = data.get(f"{packet_type}_packets")
    if val is not None:
        try:
            return int(val)
        except KeyError:
            return None

    # Case2: Ethtool output does not provide these fields, so we need to sum
    # the queues manually.
    total_packets = 0
    prefix = f"{packet_type}_queue_"
    packet_suffix = "_xdp_packets"
    any_match = False

    for k, v in data.items():
        if k.startswith(prefix) and k.endswith(packet_suffix):
            try:
                total_packets += int(v)
            except KeyError:
                return None
            any_match = True
    if not any_match:
        return None
    return total_packets


def ethtool_stat_get_startend(
    parsed_data: dict[str, int],
    ethtool_data: str,
    suffix: typing.Literal["start", "end"],
) -> bool:
    ethtool_dict = ethtool_stat_parse(ethtool_data)
    success = True
    KEY_NAMES = {
        "start": {
            "rx": "rx_start",
            "tx": "tx_start",
        },
        "end": {
            "rx": "rx_end",
            "tx": "tx_end",
        },
    }
    for ethtool_name in ("rx", "tx"):
        # Don't construct key_name as f"{ethtool_name}_{suffix}", because the
        # keys should appear verbatim in source code, so we can grep for them.
        key_name = KEY_NAMES[suffix][ethtool_name]
        v = ethtool_stat_get_packets(ethtool_dict, ethtool_name)
        if v is None:
            success = False
            continue
        parsed_data[key_name] = v
    return success


def no_traffic_on_vf_rep(
    rx_start: int, tx_start: int, rx_end: int, tx_end: int
) -> bool:
    return (
        rx_end - rx_start < VF_REP_TRAFFIC_THRESHOLD
        and tx_end - tx_start < VF_REP_TRAFFIC_THRESHOLD
    )


class PluginValidateOffload(pluginbase.Plugin):
    PLUGIN_NAME = "validate_offload"

    def _enable(
        self,
        *,
        ts: TestSettings,
        node_server_name: str,
        node_client_name: str,
        perf_server: perf.PerfServer,
        perf_client: perf.PerfClient,
        tenant: bool,
    ) -> list[PluginTask]:
        # TODO allow this to run on each individual server + client pairs.
        return [
            TaskValidateOffload(ts, perf_server, tenant),
            TaskValidateOffload(ts, perf_client, tenant),
        ]


plugin = PluginValidateOffload()


class TaskValidateOffload(PluginTask):
    @property
    def plugin(self) -> pluginbase.Plugin:
        return plugin

    def __init__(
        self,
        ts: TestSettings,
        perf_instance: perf.PerfServer | perf.PerfClient,
        tenant: bool,
    ):
        super().__init__(ts, 0, perf_instance.node_name, tenant)

        self.in_file_template = "./manifests/tools-pod.yaml.j2"
        self.out_file_yaml = (
            f"./manifests/yamls/tools-pod-{self.node_name}-validate-offload.yaml"
        )
        self.pod_name = f"tools-pod-{self.node_name}-validate-offload"
        self._perf_instance = perf_instance
        self.perf_pod_name = perf_instance.pod_name
        self.perf_pod_type = perf_instance.pod_type

    def get_template_args(self) -> dict[str, str]:
        return {
            **super().get_template_args(),
            "pod_name": self.pod_name,
            "test_image": tftbase.get_tft_test_image(),
        }

    def initialize(self) -> None:
        super().initialize()
        self.render_file("Server Pod Yaml")

    def extract_vf_rep(self) -> Optional[str]:
        r = self.run_oc_exec(
            f"crictl --runtime-endpoint=unix:///host/run/crio/crio.sock ps -a --name={self.perf_pod_name} -o json"
        )

        iface: Optional[str] = None
        if r.success:
            try:
                data = json.loads(r.out)
                v = data["containers"][0]["podSandboxId"][:15]
                if isinstance(v, str) and v:
                    iface = v
            except Exception:
                pass
            if iface is None:
                logger.info("Error parsing VF representor")
            else:
                logger.info(f"The VF representor is: {iface}")

        return iface

    def _create_task_operation(self) -> TaskOperation:
        def _thread_action() -> BaseOutput:
            self.ts.clmo_barrier.wait()

            success_result = True
            msg: Optional[str] = None
            ethtool_cmd = ""
            parsed_data: dict[str, int] = {}

            if self.perf_pod_type == PodType.HOSTBACKED:
                logger.info("The VF representor is: ovn-k8s-mp0")
                msg = "Hostbacked pod"
            elif self.perf_pod_name == perf.EXTERNAL_PERF_SERVER:
                logger.info("There is no VF on an external server")
                msg = "External Iperf Server"
            else:
                data1 = ""
                data2 = ""
                ethtool_cmd = "ethtool -S VF_REP"

                vf_rep = self.extract_vf_rep()

                if vf_rep is not None:
                    ethtool_cmd = f"ethtool -S {vf_rep}"

                    r1 = self.run_oc_exec(ethtool_cmd)

                    self.ts.event_client_finished.wait()

                    r2 = self.run_oc_exec(ethtool_cmd)

                    if r1.success:
                        data1 = r1.out
                    if r2.success:
                        data2 = r2.out

                    if not r1.success:
                        success_result = False
                    if not r2.success:
                        success_result = False

                    if not ethtool_stat_get_startend(parsed_data, data1, "start"):
                        success_result = False
                    if not ethtool_stat_get_startend(parsed_data, data2, "end"):
                        success_result = False

                logger.info(
                    f"rx_packet_start: {parsed_data.get('rx_start', 'N/A')}\n"
                    f"tx_packet_start: {parsed_data.get('tx_start', 'N/A')}\n"
                    f"rx_packet_end: {parsed_data.get('rx_end', 'N/A')}\n"
                    f"tx_packet_end: {parsed_data.get('tx_end', 'N/A')}\n"
                )

                if success_result:
                    if not no_traffic_on_vf_rep(
                        rx_start=common.dict_get_typed(parsed_data, "rx_start", int),
                        tx_start=common.dict_get_typed(parsed_data, "tx_start", int),
                        rx_end=common.dict_get_typed(parsed_data, "rx_end", int),
                        tx_end=common.dict_get_typed(parsed_data, "tx_end", int),
                    ):
                        success_result = False
                        msg = "no traffic on VF rep detected"

            return PluginOutput(
                success=success_result,
                msg=msg,
                plugin_metadata=self.get_plugin_metadata(),
                command=ethtool_cmd,
                result=parsed_data,
            )

        return TaskOperation(
            log_name=self.log_name,
            thread_action=_thread_action,
        )

    def _aggregate_output(
        self,
        result: tftbase.AggregatableOutput,
        out: tftbase.TftAggregateOutput,
    ) -> None:
        assert isinstance(result, PluginOutput)

        out.plugins.append(result)

        if self.perf_pod_type == PodType.HOSTBACKED:
            if isinstance(self._perf_instance, perf.PerfClient):
                logger.info("The client VF representor ovn-k8s-mp0_0 does not exist")
            else:
                logger.info("The server VF representor ovn-k8s-mp0_0 does not exist")

        logger.info(f"validateOffload results on {self.perf_pod_name}: {result.result}")
