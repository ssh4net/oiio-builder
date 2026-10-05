from __future__ import annotations

import re
from pathlib import Path


# GNU libtool's Darwin archive extraction lock uses hard links, independently
# of need_locks/--disable-libtool-lock. SMB mounts can reject every ln attempt,
# which libtool mistakes for contention and retries forever.
_ARCHIVE_EXTRACTION = r'''func_extract_an_archive ()
{
    $debug_cmd

    f_ex_an_ar_dir=$1; shift
    f_ex_an_ar_oldlib=$1
    if test yes = "$lock_old_archive_extraction"; then
      lockfile=$f_ex_an_ar_oldlib.lock
      until $opt_dry_run || ln "$progpath" "$lockfile" 2>/dev/null; do
	func_echo "Waiting for $lockfile to be removed"
	sleep 2
      done
    fi
    func_show_eval "(cd \$f_ex_an_ar_dir && $AR x \"\$f_ex_an_ar_oldlib\")" \
		   'stat=$?; rm -f "$lockfile"; exit $stat'
    if test yes = "$lock_old_archive_extraction"; then
      $opt_dry_run || rm -f "$lockfile"
    fi
    if ($AR t "$f_ex_an_ar_oldlib" | sort | sort -uc >/dev/null 2>&1); then
     :
    else
      func_fatal_error "object name conflicts in archive: $f_ex_an_ar_dir/$f_ex_an_ar_oldlib"
    fi
}'''

_PATCH_MARKER = "# oiio-builder: directory-based archive extraction lock (v1)"
_DIRECTORY_ARCHIVE_EXTRACTION = r'''func_extract_an_archive ()
{
  # oiio-builder: directory-based archive extraction lock (v1)
  # Scope cleanup traps to this extraction, not the rest of libtool.
  (
    $debug_cmd

    f_ex_an_ar_dir=$1; shift
    f_ex_an_ar_oldlib=$1
    f_ex_an_ar_lock_owned=
    trap 'test -z "$f_ex_an_ar_lock_owned" || rmdir "$f_ex_an_ar_lock_owned"' 0
    trap 'exit $EXIT_FAILURE' 1 2 15
    if test yes = "$lock_old_archive_extraction"; then
      lockfile=$f_ex_an_ar_oldlib.lock
      f_ex_an_ar_waits=0
      until $opt_dry_run || mkdir "$lockfile" 2>/dev/null; do
        # Bound retries for stale locks and other filesystem failures, too.
        if test "$f_ex_an_ar_waits" -ge 60; then
          func_fatal_error "Cannot acquire archive extraction lock $lockfile (stale lock or filesystem error)"
        fi
        func_echo "Waiting for $lockfile to be removed"
        sleep 2
        f_ex_an_ar_waits=$((f_ex_an_ar_waits + 1))
      done
      $opt_dry_run || f_ex_an_ar_lock_owned=$lockfile
    fi
    func_show_eval "(cd \"\$f_ex_an_ar_dir\" && $AR x \"\$f_ex_an_ar_oldlib\")" 'exit $?'
    if ($AR t "$f_ex_an_ar_oldlib" | sort | sort -uc >/dev/null 2>&1); then
      :
    else
      func_fatal_error "object name conflicts in archive: $f_ex_an_ar_dir/$f_ex_an_ar_oldlib"
    fi
  ) || exit $?
}'''


def patch_archive_locks(build_dir: Path, *, dry_run: bool = False) -> bool:
    """Keep generated libtool archive locks safe on filesystems without links.

    Only the archive-extraction function is replaced; compiler locking and
    symlink/copy capability detection remain libtool's responsibility. Configure
    regenerates libtool, so call this before make in both build/install paths.
    """
    libtool = build_dir / "libtool"
    if dry_run or not libtool.is_file():
        return False
    text = libtool.read_text(encoding="utf-8")
    if _PATCH_MARKER in text:
        return False
    if not re.search(r"(?m)^lock_old_archive_extraction=['\"]?yes['\"]?\s*$", text):
        return False
    function = re.search(r"(?ms)^func_extract_an_archive \(\)\n\{\n.*?^\}", text)
    if function is None or " ".join(function.group().split()) != " ".join(_ARCHIVE_EXTRACTION.split()):
        raise RuntimeError(f"Unsupported libtool archive-extraction function in {libtool}")
    libtool.write_text(
        text[:function.start()] + _DIRECTORY_ARCHIVE_EXTRACTION + text[function.end():],
        encoding="utf-8",
    )
    return True
