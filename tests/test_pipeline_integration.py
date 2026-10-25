from npsi.features.flow_baseline import FlowStatFeaturePipeline
from npsi.features.pipeline import NPsiFeaturePipeline
from npsi.sim.network_model import BurstEvent, LinkConfig, NetworkModel
from npsi.telemetry.simulator import tick_to_events

DT_S = 0.005


def _run_controlled_scenario():
    """One link, no background load, a single guaranteed-overflow burst
    starting well after t=0 so we have clean calm windows before it."""
    link = LinkConfig(link_id="L1", capacity_pps=100, max_backlog_pkts=20, base_load_pps=0.0)
    burst = BurstEvent(start_s=0.3, duration_s=0.2, link_id="L1", num_sources=10, per_source_pps=50)
    model = NetworkModel(links=[link], bursts=[burst], seed=1)

    npsi_pipeline = NPsiFeaturePipeline(dt_s=DT_S, window_s=0.05)
    flow_pipeline = FlowStatFeaturePipeline(dt_s=DT_S, window_s=0.25, sample_stride=2, report_delay_windows=0)

    npsi_fvs = []
    flow_fvs = []
    for tick_results in model.run(duration_s=1.0, dt_s=DT_S):
        events = tick_to_events(tick_results, DT_S)
        npsi_fvs.extend(npsi_pipeline.process_batch(events))
        flow_fvs.extend(flow_pipeline.process_batch(events))
    return npsi_fvs, flow_fvs


def test_npsi_score_rises_during_burst_and_is_zero_before():
    npsi_fvs, _ = _run_controlled_scenario()
    assert len(npsi_fvs) > 0

    pre_burst = [fv for fv in npsi_fvs if fv.ts_ns < 0.25e9]
    during_or_after = [fv for fv in npsi_fvs if 0.4e9 <= fv.ts_ns < 0.6e9]

    assert pre_burst, "expected some windows before the burst"
    assert all(fv.npsi_some == 0.0 for fv in pre_burst)

    assert during_or_after, "expected some windows during/after the burst"
    assert any(fv.npsi_some > 0.0 for fv in during_or_after)
    assert any(fv.npsi_full > 0.0 for fv in during_or_after), "burst should overflow this tiny buffer"


def test_backlog_ratio_reaches_overflow_during_burst():
    npsi_fvs, _ = _run_controlled_scenario()
    peak_ratio = max(fv.backlog_ratio_last for fv in npsi_fvs)
    assert peak_ratio >= 0.9  # 10*50=500pps arrival vs 100pps capacity should fill a 20-packet buffer fast


def test_flow_baseline_pipeline_produces_coarser_fewer_windows():
    npsi_fvs, flow_fvs = _run_controlled_scenario()
    assert len(flow_fvs) > 0
    assert len(flow_fvs) < len(npsi_fvs)


def test_flow_baseline_never_sees_stall_only_utilization():
    _, flow_fvs = _run_controlled_scenario()
    for fv in flow_fvs:
        assert 0.0 <= fv.avg_utilization <= 1.0
