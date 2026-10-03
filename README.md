# Resume Builder - Go Renderer

This renderer turns resume data into a PDF using LaTeX templates.

## How it works

1. Resume content is stored as JSON matching `resume.schema.json`.
2. `render.go` fills a LaTeX templat with that data.
3. `pdflatex` turns the result into a PDF.

## Usage

Fill the template:
  go run render.go jake.tex.tmpl jake-ryan.json jake-ryan.tex

Build the PDF:
  docker run --rm -v "${PWD}:/work" resume-latex pdflatex -interaction=nonstopmode jake-ryan.tex


The resume template is based on [Jake's Resume](https://github.com/jakegut/resume)
by Jake Gutierrez, licensed under the MIT License.
