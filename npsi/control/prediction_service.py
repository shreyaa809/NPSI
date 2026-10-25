"""
Long-running process for the real deployment: pulls telemetry (eBPF or, for
local development without a live kernel/Mininet, the simulator backend),
runs the N-PSI feature pipeline, scores each window with the trained model,
and POSTs the resulting P(congestion) to the Ryu app's REST endpoint. This
is the process that closes the loop in a real deployment, playing the same
role `SimController` plays in the Windows simulation demo.

Run against a live Mininet/OVS + Ryu setup:
    python -m npsi.control.prediction_service --backend ebpf \
        --interfaces s1-eth1 s1-eth2 --model-path models/npsi_xgboost.json \
        --ryu-url http://127.0.0.1:8080

Or against the simulator (useful to sanity-check the REST wiring without a
live kernel):
    python -m npsi.control.prediction_service --backend simulator \
        --model-path models/npsi_xgboost.json --ryu-url http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import logging

from npsi.config import DT_S, FLOW_WINDOW_S, NPSI_WINDOW_S
from npsi.features.flow_baseline import FlowStatFeaturePipeline
from npsi.features.pipeline import NPsiFeaturePipeline
from npsi.ml.infer import NPsiPredictor
from npsi.telemetry.collector import make_collector

logger = logging.getLogger("npsi.prediction_service")


def run(collector, pipeline: NPsiFeaturePipeline | FlowStatFeaturePipeline, predictor: NPsiPredictor, ryu_url: str,
        duration_s: float | None = None) -> None:
    import requests  # deferred: only required for this real-deployment entry point

    session = requests.Session()
    for batch in collector.run(duration_s):
        for fv in pipeline.process_batch(batch):
            score = predictor.predict_one(fv)
            try:
                resp = session.post(f"{ryu_url}/npsi/prediction",
                                     json={"link_id": fv.link_id, "score": score}, timeout=1.0)
                if resp.status_code != 200:
                    logger.warning("controller rejected prediction for %s: HTTP %s", fv.link_id, resp.status_code)
            except requests.RequestException as exc:
                logger.warning("could not reach Ryu controller at %s: %s", ryu_url, exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["simulator", "ebpf"], default="ebpf")
    ap.add_argument("--pipeline", choices=["npsi", "flow_stat"], default="npsi",
                     help="npsi: fine-grained N-PSI features. flow_stat: coarse NetFlow/sFlow-style "
                          "baseline features -- used to run the flow-stat baseline against the real testbed.")
    ap.add_argument("--interfaces", nargs="+", help="required for --backend ebpf")
    ap.add_argument("--model-path", type=str, default=None,
                     help="default: models/npsi_xgboost.json or models/flow_baseline_xgboost.json, matching --pipeline")
    ap.add_argument("--ryu-url", type=str, default="http://127.0.0.1:8080")
    ap.add_argument("--dt-s", type=float, default=DT_S)
    ap.add_argument("--window-s", type=float, default=None, help="default: matches --pipeline's config.py window")
    ap.add_argument("--duration", type=float, default=None, help="stop after N seconds (default: run forever)")
    args = ap.parse_args()

    collector_kwargs: dict = {"dt_s": args.dt_s}
    if args.backend == "ebpf":
        if not args.interfaces:
            ap.error("--interfaces is required for --backend ebpf")
        collector_kwargs["interfaces"] = args.interfaces
    else:
        collector_kwargs["duration_s"] = args.duration or 3600.0

    collector = make_collector(args.backend, **collector_kwargs)

    if args.pipeline == "npsi":
        window_s = args.window_s or NPSI_WINDOW_S
        pipeline = NPsiFeaturePipeline(dt_s=args.dt_s, window_s=window_s)
        model_path = args.model_path or "models/npsi_xgboost.json"
    else:
        window_s = args.window_s or FLOW_WINDOW_S
        pipeline = FlowStatFeaturePipeline(dt_s=args.dt_s, window_s=window_s)
        model_path = args.model_path or "models/flow_baseline_xgboost.json"
    predictor = NPsiPredictor(model_path)

    logger.info("starting prediction service: backend=%s pipeline=%s model=%s ryu=%s",
                args.backend, args.pipeline, model_path, args.ryu_url)
    try:
        run(collector, pipeline, predictor, args.ryu_url, duration_s=args.duration)
    except KeyboardInterrupt:
        logger.info("stopped by user")
    finally:
        close = getattr(collector, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
