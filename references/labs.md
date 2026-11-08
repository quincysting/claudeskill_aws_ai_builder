# Working through an AWS SimuLearn or FDE exam lab

What went wrong or saved time in timed, hands-on AWS lab sessions, SimuLearn and FDE among them.
Lab tags in brackets name the lab that showed each rule: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents: snapshot first, plan the run, find out what your role can do, console editors and forms, working through a browser IDE, notebooks, snapshot tricks without the CLI, reading the lab material, checking agent labs end to end.

## Snapshot before Validate or End Exam Lab

1. Save every Lambda's code, each state machine definition, notebook, template, agent code, evaluator and prompt, plus the config values and the result numbers. Put them in `code/lab_snapshot/` (FDE2 sessions use `code/lab_*`).
2. Do it before the pass. SimuLearn labs tore down within minutes of a pass (1 to 6 minutes in labs 18 to 22), and one session revoked the console credentials the moment the DIY validation passed, so the Lambda list, editor and batch list all failed with an invalid token [18, 22, FDE2-4].
3. Mask account IDs, bucket suffixes, resource IDs, domains and anything named `*SECRET*` in what you save [FDE2-7].
4. Record what you could not copy, for example functions whose `lambda:GetFunction` is denied by an SCP [FDE2-5].
5. Test what the task actually asks. Validators check that resources exist, not that they work. Lab 9 passed with a model its role denied, lab 10 passed with broken quizzes, and lab 21 passed while its pipeline rewarded toxic text [9, 10, 21].

## Plan the run

- Read every challenge and success criterion first. A step that looks cosmetic can be graded: removing an unused async destination was an explicit criterion [FDE1-5].
- Start the slow work early and do console tasks while it runs. Glue jobs, Knowledge Base creation and sync, MSK cluster config (30 minutes or more) and MSK Connect updates take minutes [FDE1-1, FDE1-2, FDE1-5].
- Batch changes to one resource into one update. One gateway edit for client ID, target ARN and new target, and one orchestrator update for client ID, env vars and VPC mode, saved several redeploy waits [FDE1-3].
- Order work that touches the same table: finish a rollback before another job changes the table's properties, and run schema fix, then partition fix, then re-ingest [FDE1-1].
- Use the exact name the task gives for anything you create. A new stream named with the lab's random suffix failed a checker that looked up the bare name [FDE1-5].
- Click Check as soon as each challenge is done. Its messages are specific, for example "missing ASSOCIATE on one or more LF-Tags" [FDE1-1]. Grader output can be stale for 15 to 30 s after a click, so read it again before acting on a failure [FDE1-5].
- In SimuLearn sessions, Practice shows every DIY defect with a reproducible command, so read it first [FDE2-1]. Don't skip the Practice deploy: one DIY grader counted models across both sections and failed until the Practice model existed [FDE2-3].
- Follow the spec's dependency order exactly (callee first, then the ARN constant, client, type copies, wiring, then redeploy). Out of order, the errors show up several steps later and point elsewhere [FDE2-2].
- Re-run the traffic or the transfer right before Validate, so the grader sees a fresh trace ID [FDE2-7]. Plan traffic runs to serve every check: one failing run that drives the alarm to ALARM, then one fixed run that brings it back to OK [FDE2-6].
- A validation form may show a placeholder name. Enter the real numbered name (`diy_eval_1`, not `diy_eval`) [FDE2-4].
- Leave permission removals (detaching an IAM policy) for last. Auto mode treats them as permission changes that need a human [FDE2-1].

## Find out what your role can do

- Probe before you plan. In one session `DescribeTable` and `ListRoles` were denied while `Scan`, `GetItem`, `PutItem` and the AgentCore control plane worked, so table keys came from the spec [FDE2-2]. In another, the workstation could manage AgentCore and read Cognito but not write SSM or Cognito [FDE1-4].
- Expect CloudShell to be disabled and plan to use the console or a lab IDE [FDE1-1, FDE1-3, FDE1-5, FDE2-4].
- `iam:PassRole` denied can mean a wrong role name, not a missing permission. A guessed `..._exec_role_...` was really `..._execution_role_...`; copy role names from IAM [FDE2-2].
- Some verify calls are denied even when create works (`logs:DescribeMetricFilters`), so check the result another way [FDE2-6].
- When the CLI can do it, use the CLI for runtimes: one `create-agent-runtime` call sets the artifact, authorizer (clients, scopes, custom claims), env vars and the header allowlist, and the console can't set the allowlist [FDE2-7]. A script created four runtimes and waited for READY in a few minutes [FDE2-2].
- Many `update-*` calls replace the whole config. Build them from the matching `get-*` output. `get-agent-runtime` can return `protocolConfiguration: null`; set `{"serverProtocol": "HTTP"}` explicitly or the CLI rejects the update [FDE1-4].
- In a Session Manager terminal, `sudo su - ec2-user` first, and chain slow creates with a wait loop that also stops on failure [FDE1-4]:

```bash
for i in $(seq 120); do   # 10 minutes at most
  s=$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW" --query status --output text)
  case "$s" in READY) break ;; *FAIL*|*UNSUCCESSFUL*) echo "gateway $s"; break ;; esac
  sleep 5
done
```

## Console editors and forms

- Read each setting back after saving. A save from a background tab did nothing, and iframes and editors drop typed text [8].
- Check the defaults: the default VPC, the wrong subnet, an SSH rule [2], private subnets and the default security group in ECS [20]. The VPC endpoint wizard quietly fell back to the default security group when a click missed; check the endpoint's Security groups tab [FDE1-3].
- In an IAM role picker, choose the role ARN entry. "Use: typed text" stores a bare name, which fails ARN validation [22].
- Ace editors (IAM JSON, SNS filter policy, Glue schema, MSK config): set the value through the editor object, then type a space and Backspace so the form state picks it up. Without the keystroke, Save can submit the old value [FDE1-4, FDE1-5].

```js
const ed = document.querySelector('.ace_editor').env.editor;
ed.setValue(JSON.stringify(policy, null, 2), 1);   // then click in, type " " and Backspace
```

- In the IAM JSON editor, change only the target `Sid` and leave the lab's other statements alone. "Review and save" loads lazily: wait for the permissions summary before clicking, then check that Policy versions went up by one [FDE1-4].
- The Lambda code editor is a cross-origin iframe, so scripts can't reach it. Use the keyboard: Ctrl+G goes to a line, Ctrl+H and Ctrl+Alt+Enter replace all, Ctrl+/ toggles a comment, Ctrl+Shift+U deploys. Find and replace is safer than typing, which auto-indents and auto-closes brackets. A deploy can hang in full-screen mode [FDE1-4, FDE1-5].
- In the Lambda test-event editor, Ctrl+A, Delete, then type one-line JSON. Test runs the modified event without saving it, and the page text can still show the old event [FDE2-4]. In the env var and JSON editors, Shift+Alt+F formats the JSON and doubles as a syntax check [FDE1-2].
- To replace a plaintext secret in env vars, rename the key (`DB_PASSWORD` to `DB_PASSWORD_SECRET_ARN`) and paste the secret's ARN as the value [FDE1-2].
- Modal dialogs (guardrail PII and regex) often need a second Confirm click, because the first only blurs the field. Escape can clear an S3 URI field. Number and ARN inputs can append to the placeholder, so select all before typing. The Lambda ARN autocomplete on gateway targets can concatenate values [FDE1-3]. Lake Formation forms re-render while you type [FDE1-1]. Re-check each value before saving.
- Glue Studio: Save before Run, and reopen the job before reading another job's script, because the editor can keep showing the previous one. Athena: switch to the lab workgroup first (it holds the result location) and run one statement per execution [FDE1-1].
- RDS Query Editor: connect with the admin secret's ARN. Statements separated by `;` run together, but run `$$`-quoted function bodies on their own [FDE1-2].
- CloudWatch runs in an iframe that scripts and find can't see; drive it with screenshots. The alarm wizard's statistic defaults to Average, so type `Sum` and pick it [FDE1-4].
- Collapsed sections hide inputs with a zero-size box. Expand the section, scroll the input into view, then click [FDE1-5].
- The Cognito machine-to-machine wizard quietly creates a `default-m2m-resource-server-*` and gives the client its scope. Edit the client afterwards to keep only the intended scope [FDE1-4].
- A policy create can fail after the console reports success. Read the status column after every create; one Cedar policy went to Create failed with "Policy references an action that does not exist on the target gateway" even though the preview validated it [FDE2-5].
- Script clicks on radio buttons (`input[type=radio][value=ACTIVE]`) and reading `.checked` back worked better than coordinate clicks after scrolling [FDE2-5]. SimuLearn form references go stale right after page load, so find the inputs again if typed values don't appear [FDE2-6].
- Deep links to a batch evaluation's detail page failed with "Unable to load content". Open it from the list [FDE2-4].
- A pop-up's beforeunload dialog can block the other tabs. Dismiss it before you continue [22].
- A sign-in bounce can re-provision the lab with a new bucket suffix, reset env vars and a missing trigger. Check them again after any sign-in [11, 14].

## Working through a browser IDE

- code-server needs the password from the stack outputs, and you type it yourself [FDE2-1]. When a gateway has no inbound auth (check its Inbound Auth type), you can test it with `curl` and skip the IDE [FDE2-5].
- Read files without the editor: run `python3 -m http.server 8765 --bind 127.0.0.1` in the app folder and fetch `/proxy/8765/<path>` from the IDE tab. The proxy is authenticated and returns whole files. Stop the server afterwards [FDE2-1, FDE2-7].
- Three traps with that proxy: `.md` files download unless the server forces `text/plain`; CloudFront caches `/proxy/...` by path and ignores the query string, so give each output file a new name; and page-text extraction collapses whitespace, which loses Python indentation [FDE2-2].
- Patch with a script, not by hand. Back up the folder, write one Python patch per area in a quoted heredoc, assert each replacement matches once, then compile [FDE2-1, FDE2-2]:

```bash
set +H   # so "!" is not history-expanded
cp -r src src.bak
python3 - <<'PYEOF'
from pathlib import Path
p = Path("src/app.py"); s = p.read_text()
old, new = "timeout=60", "timeout=10"
assert s.count(old) == 1, s.count(old)
p.write_text(s.replace(old, new))
PYEOF
python3 -m py_compile src/app.py && md5sum src/app.py   # compare with your tested local copy
```

- Click into the terminal before every command when a person shares the IDE. Focus once moved to the Explorer and a whole command went nowhere; check that a command ran before trusting its output file [FDE2-2].
- Copy big bundles out through a temporary `<textarea>`, real Ctrl+A and Ctrl+C keystrokes, then `Get-Clipboard -Raw`, and redact secrets before saving [FDE2-7].

## Notebooks

- JupyterLab ignores synthetic clicks and key events. Use real key presses such as Shift+Enter [21].
- The contents API is the reliable way to read notebooks and files: `fetch('/api/contents/<path>?content=1', {cache: 'no-store'})`, because the browser caches GETs [21, FDE2-3].
- A conda kernel can be running base Python. Check `sys.executable` [15]. The registered kernel name can differ from the notebook metadata (`conda_pytorch` against `conda_pytorch_p310`); check `jupyter kernelspec list` [FDE2-3].
- Run long notebooks headless so a browser hiccup can't stop them, and every cell's output is saved [FDE2-3]:

```bash
nohup jupyter nbconvert --to notebook --execute --ExecutePreprocessor.kernel_name=conda_pytorch \
  --ExecutePreprocessor.timeout=-1 train.ipynb --output train.executed.ipynb &
```

- Shut down earlier kernels before a GPU notebook, or the 8-bit load and training run out of memory. Check with `nvidia-smi` [FDE2-3].
- `aws sagemaker wait endpoint-in-service --endpoint-name NAME` blocks until the endpoint is ready or fails, which beats a sleep loop. The model shows up in `list-models` within seconds, long before the endpoint is InService [FDE2-3].
- The browser automation layer blocks results that look like tokens or query strings. Return parsed numbers, not raw text [FDE2-3]. Keep each automation batch under about a minute of waits [FDE2-3, FDE2-6].

## Snapshot tricks without the CLI

- CloudFormation, stack, Template tab, Copy template, then `Get-Clipboard -Raw` saves the whole template, and inline Lambdas can be extracted from it [FDE1-5, FDE2-7].
- Lambda, Download, "Download both" gives the code plus the SAM `template.yaml` [FDE2-5].
- S3-hosted Lambda code needs "Download function code", which fails when `lambda:GetFunction` is denied [FDE1-5].
- A SHA-256 of the deployed Lambda zip proves which code is running [6].

## Reading the lab material

- The lab text drifts from the deployed code and the console: renamed labels [12], wrong line numbers and an `eval` that isn't there [10], and wrong stage order and date formats [22]. Trust what you observe.
- Lab helper code is often buggy: `hits[1]` instead of `hits[0]` [17], `reward[0]` on sorted labels [21], and substring job reuse that can pick a FAILED job [13]. Read it before you reuse it.
- Read a provided Lambda before running it. One showed the event format, that evaluators are IDs, and that the graded judge is appended automatically, which turned the DIY from trial and error into one run [FDE2-4].
- A spec that says "sub=username" can be wrong for the IdP. The Keycloak `sub` was a UUID, and the checker matched `preferred_username` [FDE2-7].
- Read the hints literally: "editing the file changes nothing until you re-run the seed" and "a fix you haven't published won't pass" [FDE2-1].

## Checking agent labs end to end

- Test through the real UI: one tool question, one Knowledge Base question and one stated preference. Then open a new session and ask what it remembers [FDE1-3].
- A helper test can fail on an MCP protocol-version mismatch even when auth worked. If `initialize` returns HTTP 200, the authorizer is fine [FDE1-4].
- Turning on policy enforcement blocked the read-only tool at once, because enforcement is default deny. Write the permits for safe tools before switching the gateway to enforce [FDE2-5].
- Gateway protocol errors come back as HTTP 400 with a JSON-RPC body. Parse the body instead of raising on the status [FDE2-5].
