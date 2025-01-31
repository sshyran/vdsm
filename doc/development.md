<!--
SPDX-FileCopyrightText: Red Hat, Inc.
SPDX-License-Identifier: GPL-2.0-or-later
-->

# Development

## Environment setup

Enable oVirt packages for Fedora:

    sudo dnf copr enable -y nsoffer/ioprocess-preview
    sudo dnf copr enable -y nsoffer/ovirt-imageio-preview

Enable
[virt-preview](https://copr.fedorainfracloud.org/coprs/g/virtmaint-sig/virt-preview/)
repository to obtain latest qemu and libvirt versions:

    sudo dnf copr enable @virtmaint-sig/virt-preview

Update the system after enabling all repositories:

    sudo dnf update -y

Fork the project on https://github.com/oVirt/vdsm.

Clone your fork:

    sudo dnf install -y git
    git clone git@github.com:{your_username}/vdsm.git

Install additional packages for Fedora, CentOS, and RHEL:

    contrib/install-pkg.sh

Generate the Makefile (and configure script):

    ./autogen.sh --system --enable-timestamp

Now you can create the virtual environment
(https://docs.python.org/3/library/venv.html), which is necessary to run the
tests later. This needs to be done only once:

    make venv


## Building Vdsm

Before building, it is recommended to recreate the Makefile because it
contains version numbers, which might have changed by updating the local
repository:

    ./autogen.sh --system --enable-timestamp

To build Vdsm:

    make

To create the RPMs:

    make rpm

To upgrade your system with local build's RPM (before you do this you should
activate maintenance mode for Vdsm):

    make upgrade


## Running the tests

To run tests, first enter the virtual environment:

    source ~/.venv/vdsm/bin/activate

Then start some tests with tox, for example the networking tests:

    tox -e network

To exit the virtual environment afterwards:

    deactivate

For more information about testing see [/tests/README.md](/tests/README.md).


## Making new releases

Release process of Vdsm version `VERSION` consists of the following
steps:

- Changing `Version:` field value in `vdsm.spec.in` to `VERSION`.

- Updating `%changelog` line in `vdsm.spec.in` to the current date,
  the committer, and `VERSION`.

- Committing these changes, with subject "New release: `VERSION`" and
  posting the patch to GitHub.

- Verifying the patch by checking that the GitHub CI build produced a
  correct set of rpm's with the correct version.

- Merging the patch (no review needed).

- Tagging the commit immediately after merge with an annotated tag:
  `git tag -a vVERSION`

- Making a new release in the GitHub repo.


## CI

Running tests locally is convenient, but before your changes can be
merged, we need to test them on all supported distributions and
architectures.

When you push patches to GitHub, CI will run its tests according to the
configuration in the `.github/workflows/ci.yml` file.


## Advanced Configuration

Before running `make` you could use `./configure` to set some (rarely used) options.
To see the list of options: `./configure -h`.


## SPDX headers

All files must include the SPDX copyright notice and the license identifier.
This project employs [reuse](https://reuse.software/) to handle copyright
notices and ensure that all files have the proper SPDX headers.
To add the SPDX headers to new files in the project you can use:

    contrib/add-spdx-header.sh new_file.py

This will create default `GPL-2.0-or-later` license header
with `Red Hat, Inc.` as copyright holder.

```
# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later
```
To add new license to be used in the project:

    reuse download <License-Identifier>

Check list of available license identifier in https://spdx.org/licenses/.

To add SPDX header to a file with a non-default license:

    reuse addheader
      --copyright="Red Hat, Inc." \
      --license="<License-Identifier>" \
      --template=vdsm.jinja2 \
      --exclude-year \
      new_file.py

Please check that all files are reuse-compliant before pushing your branch:

    make reuse
