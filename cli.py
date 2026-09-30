"""
Terminal client — quickest way to try the agent without the web UI.

  export DO_MODEL_ACCESS_KEY=...
  python cli.py CASE-001 "What is the root cause?"
  python cli.py CASE-001            # interactive, one case
  python cli.py all "my card was charged twice and no confirmation email"   # bot finds the case
  python cli.py all                 # interactive, cross-case
"""
import sys

from app import agent, dataset


def ask(case_id: str, question: str, history: list) -> str:
    answer = ""
    for ev in agent.investigate(case_id, question, history):
        if ev["type"] == "tool_call":
            print(f"  ▸ {ev['name']}({ev['args']})", flush=True)
        elif ev["type"] == "tool_result":
            print(f"    ✓ {ev['chars']} chars", flush=True)
        elif ev["type"] == "answer":
            answer = ev["content"]
            print("\n" + answer + "\n")
        elif ev["type"] == "error":
            print("ERROR:", ev["message"])
    return answer


def main():
    if len(sys.argv) < 2:
        for c in dataset.list_cases():
            print(f"{c['case_id']}  [{c['affected_area']}]  {c['description'][:90]}")
        print("\nusage: python cli.py CASE-00N|all [question]")
        return
    case_id = None if sys.argv[1].lower() in ("all", "auto") else sys.argv[1].upper()
    if case_id:
        case = dataset.get_case(case_id)
        print(f"\n{case_id} — {case['description']}\n")
    else:
        print("\nCross-case mode: the bot will locate the relevant case(s) itself.\n")
    history: list = []
    if len(sys.argv) > 2:
        ask(case_id, " ".join(sys.argv[2:]), history)
        return
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        a = ask(case_id, q, history)
        if a:
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]


if __name__ == "__main__":
    main()
