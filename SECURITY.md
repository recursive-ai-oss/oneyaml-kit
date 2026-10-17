# Security policy

## Supported versions

Security fixes go into `main` and the next tagged release. Older releases are not patched; use
the latest.

## Reporting a vulnerability

Report it privately through GitHub: open the
[Security tab](https://github.com/recursive-ai-oss/oneyaml-kit/security) of this repository and
choose **Report a vulnerability**. Please do not open a public issue or pull request for a
vulnerability.

Include the commit or release you used, the command you ran, and the case, workload, adapter or
file that shows the problem. A reproduction you can run with one `bin/kit` or `bin/bench` command
is the most useful thing you can send.

Please give us the chance to publish a fix before you disclose the issue publicly.

## What counts

The kit runs YAML libraries against inputs that are hostile on purpose, so a library failing a
case is the kit doing its job, not a vulnerability in the kit. What we treat as a security issue
is a way for the kit's own files or tools to harm the machine or the people that run them, for
example:

- a case, workload, correction or result file that makes the kit's tools (`bin/kit`, `bin/bench`,
  `bin/gate`) run code, write outside the output directory, or read files they have no reason to
  read;
- an adapter or benchmark harness in this repository that handles its input unsafely: evaluates
  it, constructs objects or runs code from its tags, or passes it to a shell;
- a generated report or chart that carries active content from a result file or a library's
  output into a page someone opens;
- a way for a pull request to reach the CI workflow's credentials or write to the repository;
- `bin/bench-cloud` exposing the cloud credentials it uses, in its logs, its results or the
  rented machine;
- a credential, token or private key committed anywhere in the repository's history.

## What is not a vulnerability in the kit

- **A library crashing, hanging or using too much memory on a case.** Report that to the library.
  For a oneyaml library, follow its own security policy:
  [oneyaml-rust](https://github.com/recursive-ai-oss/oneyaml-rust/blob/main/SECURITY.md),
  [oneyaml-go](https://github.com/recursive-ai-oss/oneyaml-go/blob/main/SECURITY.md) or
  [oneyaml-csharp](https://github.com/recursive-ai-oss/oneyaml-csharp/blob/main/SECURITY.md).
- **An expectation that is wrong.** Open an issue, as described in
  [CONTRIBUTING.md](CONTRIBUTING.md#before-you-start).
- **An adapter running its own commands.** An adapter's manifest names a `build` and a `command`
  that the kit runs with your permissions ([manifest](docs/adapter-protocol.md#manifest)), so
  running an adapter means running its code. The kit is not a sandbox: run only adapters you
  trust, as you would any other program.
