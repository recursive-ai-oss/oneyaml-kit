#!/bin/sh
# Names the revision of a library checkout, for the build scripts and the benchmark driver, so
# they all follow one rule. Usage: library-rev.sh LANG SRC     (LANG is GO or CSHARP)
# Prints "<revision> <origin>" on one line. The origin says how the revision was found:
#   git       SRC is the top of a git checkout. Uncommitted changes to tracked files, untracked files
#             that are not ignored, or a git that cannot tell, add "+modified" to the short commit, so a
#             dirty tree never looks like a clean one. KIT_ONEYAML_<LANG>_REV is accepted here only
#             as a check: it must be a prefix of the commit and the tree must be clean.
#   supplied  SRC has no git history of its own (an export) and KIT_ONEYAML_<LANG>_REV names its
#             commit; the revision is recorded as given.
#   unknown   an export and no KIT_ONEYAML_<LANG>_REV: the revision is "devel".
# A revision that is "devel" or contains "+modified" is not one a published result may name.
set -eu
lang=${1:?usage: library-rev.sh LANG SRC}
src=${2:?usage: library-rev.sh LANG SRC}
var=KIT_ONEYAML_${lang}_REV
given=$(eval "printf '%s' \"\${$var:-}\"")
fail() { echo "$var: $1" >&2; exit 1; }
if [ -n "$given" ]; then
	case $given in
	*[!A-Za-z0-9._+-]*) fail "must hold only letters, digits and . _ + -" ;;
	devel | *+modified*) fail "$given is not a commit" ;;
	esac
fi
# Physical path: git reports the top of a checkout that way.
src=$(cd "$src" && pwd -P)
if [ "$(git -C "$src" rev-parse --show-toplevel 2>/dev/null)" = "$src" ]; then
	full=$(git -C "$src" rev-parse HEAD 2>/dev/null) || fail "cannot read the commit of $src"
	rev=$(git -C "$src" rev-parse --short=12 HEAD)
	dirty=
	if st=$(git -C "$src" status --porcelain --untracked-files=normal 2>/dev/null); then
		[ -z "$st" ] || dirty=1
	else
		dirty=1
	fi
	if [ -n "$given" ]; then
		[ -z "$dirty" ] || fail "$src has uncommitted changes, so no commit names it; build from a clean checkout"
		[ "${#given}" -ge 7 ] || fail "$given is too short to name a commit (use at least 7 characters)"
		case $full in
		"$given"*) ;;
		*) fail "$given is not the commit of $src ($rev); a checkout names its own commit" ;;
		esac
	fi
	[ -z "$dirty" ] || rev=$rev+modified
	echo "$rev git"
elif [ -n "$given" ]; then
	echo "$given supplied"
else
	echo "devel unknown"
fi
