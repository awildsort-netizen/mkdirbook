# mkdirbook

mkdirbook is my set of written poems and essays laid out as if I was still using MS-DOS.
I have this very vivid memory of sitting in front of my AST 486 in fourth grade with my
_DOS for Dummies_ book. It had a whole section about directories and files:

```
mkdir fruits
cd fruits
edit apple.txt
```

Somehow I've held on to that example (_my gods_) for over thirty years. It tickled my
brain because it showed me this platonic world of sorting through my life: "here, let
me show you how to create your very own taxonomy of fruit so you can organize your
thoughts about grapes."

### The magic in mkdir

Now, I understand. It was my first tuning into symbolic magic, on 3.25" floppy.

Raise your hand if you've gone through a hard time in your life, and you're on the other
side of it, but you still have trouble explaining exactly what happened? In my case, it
transformed me into something old, excavated from my childhood, and new, the cutting
edge of what it means to be me, at the same time? I rediscovered a connection to something
bigger, but this time its body wasn't made of hymns and Bible verses. It was made in
a different land where words lived their own lives, of tarot and physics and deepened
astrology synesthesia.

In it, I discovered a powerful magic akin to the DOS for Dummies `mkdir fruits` lesson.
That probably sounds ridiculous, but any magic practice worth its salt will show you
the magic is in the everyday parts of life. The best magic is nearly invisible, woven
into our regular rituals like sweeping floors and talking to ourselves.

It was a symbol magic. I remember observing my thoughts, thinking, "this is akin to actual
holistic Hoodoo practice, relieved of colonialism's aluminum grip of it." Dolls are symbol
magic, and they can be used for healing / joy / flow in their fuller form, the river of
thoughts sang to me. I created these DnD-like scenes of my ancestors, mappings of my left
brain and my right brain, and begin to play with them.

Beliefs are like plants, and mine took over the garden of my life, wild and exploding outwards.
This is me returning to myself, using words to bind my compressed experiences into meaningful
order.

### How to use

Run `make setup` once from the project root to create the virtual environment.

After that, each book builds from its own directory:

```sh
cd free2move
make
```

That default build writes **HTML** and **DOCX** output using the book's local manifest.

From the project root, `make gui` still opens the textual manifest manager.

### `.aswritten` whitelists and review

For intentional glitches or voice-driven spellings, use one directory-level
whitelist file named `.aswritten`.

Use `# <filename>` to switch the active file:

```text
# free2move/iphonesig.md
21: PS: {Cray??la} is now shipping

# dtune/Drift Becoming.md
13: Damn this {A D HD}
```

- if the first word on a line is an existing filename, that also switches the
  active file
- the number before `:` is an approximate line
- text outside `{}` is local context for anchoring
- text inside `{}` is the protected glitch span

To review all changed markdown/text files from `HEAD`:

```sh
./scripts/aswritten.py
```

To review only specific files:

```sh
./scripts/aswritten.py newsletters/narcfall.md free2move/vogueair.md
```

The review command:

- defaults to all changed non-dotfiles under the current directory
- can be narrowed to specific filenames on the command line
- refreshes `.aswritten` references against `HEAD`
- automatically updates good fuzzy line references
- offers to remove references it can no longer find
- auto-reverts changes that already match accepted `.aswritten` rules
- prints a note when it does that
- prompts on the remaining diff lines to either accept the current change or
  whitelist the `HEAD` version as written
- appends new whitelist rules to `.aswritten` when you choose the whitelist path
