"""Gerrit Promises: promises made in code review, and whether they were kept.

In review, an author or reviewer often defers work -- "pre-existing, will
fix in a follow-up", "I will create a patch in LU-19999" -- and the thread
is resolved and forgotten. This package tracks such promises per Gerrit
change:

1. harvest every comment thread, resolved ones included (``harvest``),
   drop threads only mechanical checkers took part in, and look for
   follow-up changes the portal can find on its own (``followups``);
2. have Claude pick out the promises from the remaining threads
   (``judge.classify_threads`` -- text only, no tools);
3. have Claude check each promise against the code: kept in a later
   patchset or on master, or still open (``judge.adjudicate`` -- reads
   a read-only snapshot, cannot run commands).

Steps 2 and 3 cost money and are started only by an admin. Everything
else is free and runs for anyone signed in, and on a schedule.

``status`` turns the stored documents into what the pages show and is
the file to read first. Nothing here imports Flask.
"""
