# Project verification profiles

Run `bin/aos-profile.sh <project>` before editing. Read project instructions and
use commands actually supported by the project. The profiler reports evidence;
it does not execute tests or establish deployment authorization.

## Node and Python

Use the detected package manager and declared lint, build and test commands.
For Python, use a compatible interpreter and the evidenced runner (unittest or
pytest). With no tests, compile changed modules and exercise safe representative
inputs in an isolated directory. Imports and --help can execute code: inspect
side effects first and avoid production credentials.

## WordPress

Check PHP syntax with an available compatible interpreter. Follow the project's
deployment tooling and instructions; do not assume any particular host, wrapper
or SSH account exists. A release needs a recoverable backup, health checks and a
rollback procedure. Verify the affected page or behavior after an authorized deploy.

## Home Assistant and workflows

Validate configuration using the target installation's supported checks before
reloading. Keep the prior configuration recoverable. Heating, charging, gates and
similar physical effects require risk-specific checks. Test workflow changes on a
safe copy or fixture; sending messages and activating production jobs need the
corresponding authorization. Do not infer authorization from sample commands.

## Documentation and static artifacts

Inspect the actual rendered output where layout matters. For instruction changes,
read the final diff and check links, examples and consistency. State when checks
validate structure rather than model behavior. A file's presence is not proof of
correct output, deployment, or independent review.
