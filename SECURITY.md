# Reporting a security fault

sortie-kit signs authority for coding agents and starts them on your machine,
so a fault in it can matter. Please report one privately.

## How to report

Use GitHub's private vulnerability reporting for this repository,
https://github.com/HakAl/sortie-kit:
open the repository's **Security** tab and choose **Report a vulnerability**.
The report is visible only to the maintainers until a fix is published.

Please do not open a public issue or pull request for a security fault, and
do not put the details in a discussion.

## What helps

- The version (`sortie --version`), the operating system and the Python
  version.
- Which agent CLIs and profiles were in the seats, built-in or your own.
- What you did, what happened and what you expected, with the smallest steps
  that show it.
- What someone could do with it: for example start a seat that was not
  signed, run past the signed window, have a review accepted that the
  reviewer did not give, or read or write outside what the summary said.

Leave keys, passphrases and tokens out of the report. A shift directory can
hold your direction file, model output and file paths; send parts of it only
if you are content for the maintainers to read them.

## What to expect

This is an alpha maintained in spare time. Reports are read and answered,
and no response time is promised. Fixes go into the newest version only;
earlier alphas are not patched.

## What is not a security fault here

A seat runs under your own configuration of its agent CLI. What that CLI
allows, and what a model does within it, is not something sortie-kit limits;
[what a shift can do to your machine](docs/machine.md) says what the
built-in profiles ask for. A report that a built-in profile asks for more
than that page says is welcome.
