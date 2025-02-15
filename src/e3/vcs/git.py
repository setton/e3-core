"""High-Level interface to Git repository.

Example::


    g = GitRepository(working_tree='/tmp/e3-core')
    g.init()
    g.update('ssh://git.adacore.com/anod', refspec='master', force=True)
    with open('/tmp/e3-core-log', 'w') as fd:
        g.write_log(fd, max_count=10)
    with open('/tmp/e3-core-log') as fd:
        authors = []
        for commit in g.parse_log(fd, max_diff_size=1024):
            authors.append(commit['email'])
"""


from __future__ import annotations

import itertools
import os
import sys
import tempfile
from contextlib import closing
from typing import TYPE_CHECKING

import e3.fs
import e3.log
import e3.os.fs
import e3.os.process
from e3.os.process import PIPE
from e3.text import bytes_as_str
from e3.vcs import VCSError

if TYPE_CHECKING:
    from typing import (
        Any,
        Final,
        IO,
        Literal,
        List,
        Optional,
        TextIO,
    )
    from collections.abc import Iterator
    from e3.os.process import Run, DEVNULL_VALUE, PIPE_VALUE

    Git_Cmd = List[Optional[str]]
    GIT_LOG_STREAM_VALUE = Literal[-4]

# Special value to direct outputs to the git log stream
# STDOUT, PIPE, and DEVNULL values are between -1 and -3
GIT_LOG_STREAM: GIT_LOG_STREAM_VALUE = -4

logger = e3.log.getLogger("vcs.git")

HEAD: Final = "HEAD"
FETCH_HEAD: Final = "FETCH_HEAD"


# Implementation note: some git commands can produce a big amount of data (e.g.
# git diff or git log). We always redirect git command result to a file then
# parse it, limiting the size of data read to avoid crashing our program.


class GitError(VCSError):
    pass


class GitRepository:
    """Interface to a Git Repository.

    :cvar git: path to the git binary
    :cvar log_stream: stream where the log commands will be redirected
        (default is stdout)
    :ivar working_tree: path to the git working tree
    """

    git: str | None = None
    log_stream: TextIO | IO[str] = sys.stdout

    def __init__(self, working_tree: str):
        """Initialize a GitRepository object.

        :param working_tree: working tree of the GitRepository
        """
        self.working_tree = working_tree

    @classmethod
    def create(cls, repo_path: str, initial_content_path: str | None = None) -> str:
        """Create a local Git repository.

        :param repo_path: a local directory where to create the repository
        :param initial_content_path: directory containing the initial content
            of the repository. If set to None an empty repository is created.
        :return: the URL of the newly created repository
        """
        repo_path = os.path.abspath(repo_path)
        repo = cls(repo_path)
        repo.init()
        if initial_content_path is not None:
            e3.fs.sync_tree(initial_content_path, repo_path, ignore=[".git"])
            repo.git_cmd(["add", "-A"])
            repo.git_cmd(["config", "user.email", "e3-core@example.net"])
            repo.git_cmd(["config", "user.name", "e3 core"])
            repo.git_cmd(["commit", "-m", "initial content"])
        return repo_path

    def git_cmd(
        self,
        cmd: Git_Cmd,
        output: DEVNULL_VALUE
        | PIPE_VALUE
        | GIT_LOG_STREAM_VALUE
        | str
        | IO
        | None = GIT_LOG_STREAM,
        **kwargs: Any,
    ) -> Run:
        """Run a git command.

        :param cmd: the command line as a list of string, all None entries will
            be discarded
        :param output: see e3.os.process.Run, by default it is the
            ``log_stream`` class attribute.
        """
        if self.__class__.git is None:
            git_binary = e3.os.process.which("git", default=None)
            if git_binary is None:  # defensive code
                raise GitError("cannot find git", "git_cmd")
            self.__class__.git = e3.os.fs.unixpath(git_binary)

        if output == GIT_LOG_STREAM:
            output = self.log_stream

        p_cmd = [arg for arg in cmd if arg is not None]
        p_cmd.insert(0, self.__class__.git)

        p = e3.os.process.Run(p_cmd, cwd=self.working_tree, output=output, **kwargs)
        if p.status != 0:
            raise GitError(
                "{} failed (exit status: {})".format(
                    e3.os.process.command_line_image(p_cmd), p.status
                ),
                origin="git_cmd",
                process=p,
            )
        return p

    def init(self, url: str | None = None, remote: str | None = "origin") -> None:
        """Initialize a new Git repository and configure the remote.

        :param url: url of the remote repository, if None create a local git
            repository
        :param remote: name of the remote to create
        :raise: GitError
        """
        e3.fs.mkdir(self.working_tree)
        self.git_cmd(["init", "-q"])

        # Git version 1.8.3.1 might crash when calling "git stash" when
        # .git/logs/refs is not created. Recent versions of git do not
        # exhibit this problem
        e3.fs.mkdir(os.path.join(self.working_tree, ".git", "logs", "refs"))

        if url is not None:
            self.git_cmd(["remote", "add", remote, url])

    def checkout(self, branch: str, force: bool = False) -> None:
        """Checkout a given refspec.

        :param branch: name of the branch to checkout
        :param force: throw away local changes if needed
        :raise: GitError
        """
        cmd = ["checkout", "-q", "-f" if force else None, branch]
        self.git_cmd(cmd)

    def describe(self, commit: str = HEAD) -> str:
        """Get a human friendly revision for the given refspec.

        :param commit: commit object to describe.
        :return: the most recent tag name with the number of additional commits
            on top of the tagged object and the abbreviated object name of the
            most recent commit (see `git help describe`).
        :raise: GitError
        """
        p = self.git_cmd(["describe", "--always", commit], output=PIPE)
        return p.out.strip()  # type: ignore

    def write_local_diff(self, stream: IO[str]) -> None:
        """Write local changes in the working tree in stream.

        :param stream: an open file descriptor
        :raise: GitError
        """
        cmd: Git_Cmd = ["--no-pager", "diff"]
        self.git_cmd(cmd, output=stream, error=self.log_stream)

    def write_diff(self, stream: IO[bytes], commit: str) -> None:
        """Write commit diff in stream.

        :param commit: revision naming a commit object, e.g. sha1 or symbolic
            refname
        :param stream: an open file descriptor
        :raise: GitError
        """
        cmd: Git_Cmd = [
            "--no-pager",
            "diff-tree",
            "--cc",
            "--no-commit-id",
            "--root",
            commit,
        ]
        self.git_cmd(cmd, output=stream, error=self.log_stream)

    def fetch(self, url: str, refspec: str | None = None) -> None:
        """Fetch remote changes.

        :param url: url of the remote repository
        :param refspec: specifies which refs to fetch and which local refs to
            update.
        :raise: GitError
        """
        self.git_cmd(["fetch", url, refspec])

    def update(self, url: str, refspec: str, force: bool = False) -> None:
        """Fetch remote changes and checkout FETCH_HEAD.

        :param url: url of the remote repository
        :param refspec: specifies which refs to fetch and which local refs to
            update.
        :param force: throw away local changes if needed
        :raise: GitError
        """
        self.fetch(url, refspec)
        self.checkout("FETCH_HEAD", force=force)

    def fetch_gerrit_notes(self, url: str) -> None:
        """Fetch notes generated by Gerrit in `refs/notes/review`.

        :param url: url of the remote repository
        """
        self.fetch(url, "refs/notes/review:refs/notes/review")

    def write_log(
        self,
        stream: IO[str],
        max_count: int = 50,
        rev_range: str | None = None,
        with_gerrit_notes: bool = False,
    ) -> None:
        """Write formatted log to a stream.

        :param stream: an open stream where to write the log content
        :param max_count: max number of commit to display
        :param rev_range: git revision range, see ``git log -h`` for details
        :param with_gerrit_notes: if True also fetch Gerrit notes containing
            review data such as Submitted-at, Submitted-by.
        :raise: GitError
        """
        # Format:
        #   %H: commit hash
        #   %aE: author email respecting .mailmap
        #   %ci: committer date, ISO 8601-like format (don't use %cI)
        #   %n: new line
        #   %B: raw body (unwrapped subject and body)
        #   %N: commit notes
        cmd: Git_Cmd = [
            "log",
            "--format=format:%H%n%aE%n%ci%n"
            + ("%N%n" if with_gerrit_notes else "")
            + "%n%B",
            "--log-size",
            "--max-count=%d" % max_count if max_count else None,
            "--show-notes=review" if with_gerrit_notes else None,
            rev_range,
        ]
        self.git_cmd(cmd, output=stream, error=None)

    def parse_log(
        self, stream: IO[str], max_diff_size: int = 0
    ) -> Iterator[dict[str, str | dict]]:
        """Parse a log stream generated with `write_log`.

        :param stream: stream of text to read
        :param max_diff_size: max size of a diff, if <= 0 diff are ignored
        :return: a generator returning commit information (directories with
            the following keys: sha, email, date, notes, message, diff). Note
            that the key diff is only set when max_diff_size is bigger than 0.
            The notes value is a dictionary built from the 'key:value' found in
            Gerrit notes.
        """

        def to_commit(object_content: str) -> dict:
            """Return commit information."""
            headers, body = object_content.split("\n\n", 1)

            # Retrieve sha, email, date, and (optionally) notes if some notes
            # are attached to the commit
            result = dict(
                itertools.zip_longest(
                    ("sha", "email", "date", "notes"),
                    headers.replace("\r", "").split("\n", 3),
                )
            )

            # replace notes "key: value" lines by a dictionary
            if result["notes"]:
                try:
                    result["notes"] = dict(
                        notes_line.split(": ", 1)
                        for notes_line in result["notes"].splitlines()
                    )
                except ValueError:
                    # Notes format invalid, discard it
                    result["notes"] = None

            result["message"] = body

            if max_diff_size > 0:
                tempfile_name = None
                try:
                    with closing(
                        tempfile.NamedTemporaryFile(mode="wb", delete=False)
                    ) as diff_fd:
                        tempfile_name = diff_fd.name
                        self.write_diff(diff_fd, result["sha"])

                        # compute file size
                        diff_fd.seek(0, 2)
                        diff_size = diff_fd.tell()
                        e3.log.debug("diff size for %s: %d", result["sha"], diff_size)

                    with open(tempfile_name, "rb") as f:
                        content = f.read(max_diff_size)

                        # Diff content is not always in utf-8 format thus use
                        # a safe function to decode it.
                        result["diff"] = bytes_as_str(content)

                    if diff_size > max_diff_size:
                        result["diff"] += "\n... diff too long ...\n"
                finally:
                    if tempfile_name is not None:
                        e3.fs.rm(tempfile_name)

            return result

        size_to_read = 0
        while True:
            line = stream.readline()
            if not line.strip():
                # Strip empty line separating two commits
                line = stream.readline()
            if line.startswith("log size "):
                size_to_read = int(line.rsplit(None, 1)[1])
                # Get commit info
            if size_to_read <= 0:
                return
            yield to_commit(stream.read(size_to_read))
            size_to_read = 0

    def rev_parse(self, refspec: str = HEAD) -> str:
        """Get the sha associated to a given refspec.

        :param refspec: refspec.
        :raise: GitError
        """
        p = self.git_cmd(["rev-parse", "--revs-only", refspec], output=PIPE, error=PIPE)
        return p.out.strip()  # type: ignore
