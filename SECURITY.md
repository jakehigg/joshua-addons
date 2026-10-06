# Security

## Report a vulnerability

Report a security problem privately, through GitHub's private vulnerability
reporting: open the **Security** tab of this repository and choose **Report a
vulnerability**. Do not open a public issue for it.

## What counts

- A way past `ADDON_TOKEN`: a request with no token or a wrong token that an
  addon answers.
- A path that reaches outside the data an addon owns.
- A token or a credential in a log, in an error message, or in a tool result.
- An addon that reaches a host its README does not name.
- A tool that does more than its description says.

## What does not count

- A wrong answer from the model.
- A tool that does what its allowlist in `joshua.yaml` permits. You choose the
  allowlist.
- A problem in a service an addon talks to, such as Discogs or a git host.
  Report it to that service.

## Response

Best effort, and no promise of a time. The fix lands in a release, with a line
in `docs/CHANGELOG.md`. `docs/architecture.md` describes the trust model and
what an addon does not promise.
