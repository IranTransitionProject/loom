# Diagram sources

`.drawio` files in this directory are the source of truth for architecture
and concept diagrams. CI auto-exports them to SVG on every change AND
generates a dark-mode variant for each so docs render correctly under both
Material's `default` and `slate` palettes.

## How it works

- `.drawio` files in this directory are exported to SVG by CI
  (`.github/workflows/build-diagrams.yml`).
- Exported SVGs land in `docs/images/` with `--embed-diagram` so they are
  re-openable in draw.io.
- `docs/diagrams/make_dark_variants.py` post-processes every `<name>.svg`
  in `docs/images/` (drawio-sourced AND Python-generated) and produces
  `<name>-dark.svg` with a palette tuned for dark backgrounds. The CI
  workflow runs the script after the drawio export so both files always
  appear together.
- Edit `.drawio` files in draw.io desktop or the web editor at
  <https://app.diagrams.net>.

## Adding a new diagram

1. Create or save the diagram as `docs/diagrams/<name>.drawio`.
2. Open a PR — the workflow exports `docs/images/<name>.svg`, runs the
   dark-variant post-processor, and commits both.
3. Reference both variants from docs:

   ```markdown
   ![Caption](images/<name>.svg#only-light)
   ![Caption](images/<name>-dark.svg#only-dark)
   ```

   The `#only-light` / `#only-dark` URL fragments are CSS hooks defined
   in `docs/stylesheets/theme-aware-images.css`; Material picks the
   right one based on the active palette.

## Python-generated diagrams

`docs/generate_diagrams.py` builds two diagrams that pre-date the
drawio toolchain (`developer-workflow.svg`, `workshop-ui.svg`). They go
through the same dark-variant post-processor, so the only manual step
to refresh them is to re-run the generator script before committing.

## Pipeline test diagram

`_pipeline-test.drawio` is a trivial 3-box-and-2-arrows diagram kept around
as the working example for contributors and to verify the export pipeline
still functions. Do not delete.
