# illreader

Read an Adobe Illustrator `.ai` file's own data, straight from the file. No Illustrator needed.

illreader is not made by or affiliated with Adobe. "Adobe" and "Illustrator" are trademarks of Adobe Inc.

## Purpose

AI coding agents can't open Illustrator. When they're handed an `.ai` file, the best they can usually do is read its PDF preview or look at a screenshot. Both lose a lot. Group names are gone. Hidden layers are gone. Live type is often flattened, so there's no font, size or leading to read. The agent ends up guessing from pixels.

Illustrator also saves its own native data inside every PDF-compatible `.ai` file. illreader reads that data and prints what the designer actually built, as plain text an agent (or a person) can read:

- Layers, sublayers and named groups, with hidden objects marked
- Live text: the copy, font (PostScript name), size, leading, tracking, alignment, CMYK color, and whether it's point or area type
- Line positions: first baseline and baseline-to-baseline spacing
- Rectangles with their corner radii
- Gradients with their stops and angle
- Placed images and guides
- Outlined type: lines of text that were converted to shapes, with each line's position and baseline

Positions are in points, measured from the artboard's top-left corner, with y going down.

It only reads. It never changes your file.

## How to

### Install

You need Python 3 (tested with 3.12).

```bash
git clone https://github.com/datatimp/illreader.git
cd illreader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead.

### Read a file

```bash
.venv/bin/python illreader.py FILE.ai
```

This prints a readable outline of the file. The outline can be long, so you may want to save it:

```bash
.venv/bin/python illreader.py FILE.ai > outline.txt
```

| Option | What it does |
|---|---|
| `--json OUT.json` | Also writes the full structure (every object, paragraph and text run) as JSON |
| `--all` | Includes small unnamed paths in the outline |
| `--dump-native OUT` | Writes the raw native data, for debugging |

### Measure outlined type

When type has been outlined, the font data is gone. `measure_type.py` works it back out. Give it the words on the line, the font, and the line's ink box from the `OUTLINED TYPE` entry in the outline:

```bash
.venv/bin/python measure_type.py --font Fraunces-Bold --text "WHY CHOOSE" --ink 63.9 28.4 305.1 54.1
```

It reports the font size, the horizontal scaling (or the tracking that would give the same width), and where the baseline sits. Against live text, it's accurate to about 0.05 pt in size and 0.5% in scaling.

```bash
# many lines at once, from a JSON list of {"font", "text", "ink"}
.venv/bin/python measure_type.py --lines lines.json

# size from one clean letter, for script or shadowed lettering
.venv/bin/python measure_type.py --font SantoroScriptJF --text "Real Good" --ink 10 20 200 60 --glyph R 11.6

# list installed fonts that match a name
.venv/bin/python measure_type.py --find fraunces
```

`--font` takes a PostScript name (`Fraunces-Bold`), a family and style (`"Fraunces Bold"`), or a path to a font file.

### Where it looks for fonts

`measure_type.py` needs the actual font file. It searches the standard font folders for your system:

- **Windows:** the system and per-user Fonts folders
- **macOS:** `/System/Library/Fonts`, `/Library/Fonts` and `~/Library/Fonts`
- **Linux:** `~/.local/share/fonts`, `~/.fonts`, `/usr/local/share/fonts` and `/usr/share/fonts`
- **WSL:** the Linux folders, plus the Windows Fonts folders through `/mnt/c`

If your fonts live somewhere else, such as a font manager's folder, add it for one run:

```bash
.venv/bin/python measure_type.py --font-dir ~/MyFonts --find fraunces
```

Or set it once in your shell profile, so every run sees it. Separate several folders with `:` (or `;` on Windows):

```bash
export ILLREADER_FONT_DIRS="$HOME/MyFonts:$HOME/ClientFonts"
```

The font list is cached in `~/.cache/illreader-fonts.json`, so later runs are fast.

### Use it with an AI agent

illreader is a plain command-line tool, so any agent that can run shell commands can use it. Claude Code, Cursor, Codex and others all can. Install it as above, then tell the agent where it is. For example:

> To read an `.ai` file, run `~/illreader/.venv/bin/python ~/illreader/illreader.py FILE.ai > outline.txt`, then read `outline.txt`. Take type specs, colors and positions from it, not from a render of the file.

Put that in the agent's standing instructions (such as `CLAUDE.md` or `AGENTS.md`) so it remembers every time.

Some tips worth passing on to the agent:

- Save the outline to a file and search it (`grep -n 'LAYER\|TEXT\|run:\|OUTLINED'`). A big file's outline is too long to read in one go.
- Skip anything marked `(HIDDEN)`. Designers often keep a hidden live-text copy next to a visible outlined one. Build from the visible one, and take type specs from the live one.
- For outlined type, look for a hidden live copy first. Only measure with `measure_type.py` if there isn't one.

## Use cases

- **Building a design in code from an Illustrator comp.** The agent gets exact type specs, colors and positions instead of guessing from a screenshot.
- **Recovering specs from outlined type.** A file arrives with the type outlined and no notes. Measure the size, scaling and baseline of each line.
- **Auditing a file before handoff.** Check for hidden layers, stray objects, fonts used only in hidden text, and colors that drifted from the brand.
- **Moving work to another tool.** Carry type specs and layer names into Figma, Affinity, ArtCraft suite, Inkscape or a web build.
- **Working without Illustrator.** Read an `.ai` file on a machine with no Adobe license.

## Limits

- The file must be saved with **Create PDF Compatible File** turned on (Illustrator's default). Without it, there's no native data to read.
- With several artboards, positions are measured from the first artboard. The header lists each other artboard's offset.
- Group names come out cleaned up: Illustrator stores spaces as `_` and adds number suffixes, and illreader strips both.
- OpenType features, kerning pairs and tab stops aren't reported.
- Outlined-type detection looks for rows of letter-sized shapes sharing a baseline. Small logo lettering shows up too, and very short words (about two letters) can be missed.
- For a variable font, `measure_type.py` measures its default instance.
- Gradients are reported by name and angle only.

Also reads files with old Mac line endings, Illustrator CS4 and earlier, and CorelDRAW's `.ai` export.

## License

MIT. See [LICENSE](LICENSE).
