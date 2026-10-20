import argparse
import time
from .auth import seed_account,issue_link
from .config import Config
from .db import Database,dump


def main():
    parser = argparse.ArgumentParser(description="PulseFeed account and operator tools; never logs links through web access logs.")
    sub = parser.add_subparsers(dest="command",required=True)
    seed = sub.add_parser("seed",help="Create evaluator/admin identity")
    seed.add_argument("--name",required=True); seed.add_argument("--role",choices=["evaluator","admin"],required=True)
    issue = sub.add_parser("issue-link",help="Print a single-use link. Treat the output as a credential.")
    issue.add_argument("--account",required=True)
    revoke = sub.add_parser("revoke",help="Revoke all access links and sessions for one account")
    revoke.add_argument("--account",required=True)
    resolve = sub.add_parser("resolve",help="Reconcile uncertain work after checking Runpod manually")
    resolve.add_argument("--run",required=True)
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--provider-id",help="Attach the confirmed provider job ID for the pending invocation")
    choice.add_argument("--confirm-stopped",action="store_true",help="Affirm manual confirmation that no provider work remains active; keep reserved quota charged")
    args = parser.parse_args()
    config = Config.from_env(); db = Database(config.database_path,config.global_daily_limit,config.feed_article_limit)
    if args.command == "seed":
        print(seed_account(db,args.name,args.role)); return
    if args.command in ("issue-link","revoke"):
        account = db.one("SELECT id FROM accounts WHERE name=? OR id=?",(args.account,args.account))
        if not account:
            parser.error("Unknown account")
        if args.command == "issue-link":
            print(issue_link(db,config,account["id"])); return
        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET revoked=1 WHERE account_id=?",(account["id"],))
            conn.execute("UPDATE access_tokens SET revoked=1 WHERE account_id=?",(account["id"],))
        print("Access revoked"); return
    with db.transaction() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?",(args.run,)).fetchone()
        if not run or run["state"] in ("failed","completed"):
            parser.error("Run must exist and be unresolved")
        invocation = conn.execute("SELECT * FROM invocations WHERE run_id=? ORDER BY created DESC LIMIT 1",(args.run,)).fetchone()
        if args.provider_id:
            if not invocation or invocation["provider_id"]:
                parser.error("Only an invocation without a known provider ID can be attached")
            conn.execute("UPDATE invocations SET provider_id=?,status='pending' WHERE id=?",(args.provider_id,invocation["id"]))
            conn.execute("UPDATE runs SET state=?,error=NULL WHERE id=?",("creating_prompt" if invocation["stage"] == "qwen" else "generating_image",args.run))
        else:
            error = {"code":"CANCELLED","message":"An operator confirmed that provider work stopped.","stage":invocation["stage"] if invocation else "qwen","retryable":False}
            conn.execute("UPDATE runs SET state='failed',error=?,quota_finalized_at=?,quota_epoch=(SELECT reset_epoch FROM quota_policy WHERE id=1) WHERE id=?",(dump(error),time.time(),args.run))
            if invocation:
                conn.execute("UPDATE invocations SET status='operator_resolved' WHERE id=?",(invocation["id"],))
            if not conn.execute("SELECT 1 FROM runs WHERE batch_id=? AND state NOT IN ('failed','completed')",(run["batch_id"],)).fetchone():
                conn.execute("UPDATE batches SET state='finished' WHERE id=?",(run["batch_id"],))
    print("Operator resolution recorded; reserved attempt accounting preserved")


if __name__ == "__main__":
    main()
