"""judge_accept -- thin CLI over judge_client.judge_verdict() for one
leaf cell: the "leaf routing" acceptance mode's "acceptance of a leaf
is ONLY by judge" mechanism.

Usage:
    python tools/judge_accept.py --cell <leaf-cell-dir> --keys keys/<node>.md \
        --task "<verbatim leaf task text>" [--stdout <stdout-tail-file>]

Prints JSON {"accept": bool, "feedback": str, "usage": {...},
"cost_usd": float|None} to stdout and exits:
    0  -- accept:true
    1  -- accept:false (reject)
    2  -- error (proxy unreachable, transport failure, unparseable judge
         reply after retry) -- an honest {"error": "..."} line on stdout,
         never a silent accept/reject.

Material assembly: build_material() runs with baseline_files=None
explicitly -- there is no separate baseline-manifest step in this CLI's
own harness -- an honest fallback marker (judge_client.BASELINE_UNAVAILABLE_MARKER)
rather than a silent unfiltered listing pretending nothing was excluded.

task_id (a judge_verdict()/build_prompt() parameter, used only for the
prompt's "Task {task_id}:" label -- it does not affect the accept/
reject decision): this CLI's argument list is fixed to
--cell/--keys/--task/--stdout with no separate --task-id flag, so
task_id is derived from the --keys file's stem -- the file is named
keys/<node>.md by convention, so its stem IS the node id already.

Intent keys: --keys file is read as one intent key per non-empty
stripped line (the literal "intent-keys file" -- a node's keys file is
prose/bullets; each line becomes one bullet judge_client.build_prompt
renders as "- <line>"). Blank lines are dropped, nothing else is
special-cased -- no markdown heading stripping, since treating every
non-empty line as a key is the simplest behavior a keys-file author can
predict.
"""

import argparse
import json
import sys
from pathlib import Path

import judge_client


def _read_keys(path):
    text = Path(path).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _read_stdout_tail(path):
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Judge acceptance of one leaf-routing DAG leaf cell")
    parser.add_argument("--cell", required=True, help="leaf cell directory (judge material source)")
    parser.add_argument("--keys", required=True, help="path to the intent-keys file (keys/<node>.md)")
    parser.add_argument("--task", required=True, help="leaf task text, verbatim")
    parser.add_argument("--stdout", default=None, help="optional path to a stdout-tail file")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_arg_parser().parse_args(argv)

    task_id = Path(args.keys).stem
    intent_keys = _read_keys(args.keys)
    stdout_tail = _read_stdout_tail(args.stdout)

    try:
        verdict = judge_client.judge_verdict(
            task_id=task_id,
            task_text=args.task,
            intent_keys=intent_keys,
            cell_dir=args.cell,
            stdout_tail=stdout_tail,
            baseline_files=None,
        )
    except Exception as exc:  # proxy unreachable / transport error / JudgeParseError
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({
        "accept": verdict["accept"],
        "feedback": verdict["feedback"],
        "usage": verdict["usage"],
        "cost_usd": verdict["cost_usd"],
    }, ensure_ascii=False))
    return 0 if verdict["accept"] else 1


if __name__ == "__main__":
    sys.exit(main())
