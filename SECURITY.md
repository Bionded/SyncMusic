# Security policy

SyncMusic runs with root rights (it mounts and formats USB disks) and stores library credentials, so security
reports matter to us.

## Reporting a vulnerability
Please **do not open a public issue**. Report it privately through GitHub's *Security → Report a vulnerability*
on [the repository](https://github.com/Bionded/SyncMusic/security/advisories/new) (private vulnerability reporting). Include the version (`syncmusic --version`), what an attacker can do, and steps to reproduce.
You get an answer within a week; a fix is released as soon as it is ready and you are credited unless you prefer not.

## Supported versions
Only the latest release gets security fixes.

## Scope notes
- The web UI and API have no login of their own: bind them to localhost or a trusted network (see the user docs,
  installation), or put them behind a reverse proxy with authentication.
- The block-device guard refusing a disk that is not a removable USB device or a known player is a security
  boundary: a way around it is a vulnerability.
