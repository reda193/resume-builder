# Resume Builder

Upload your resume, get it back in a clean LaTeX template, and tailor it to any job posting.

## How it works

1. **Extract:** Python reads your resume (PDF or Word) and the AI turns it into structured data.
2. **Profile:** your data becomes a profile you can keep adding to.
3. **Tailor:** the AI picks the best parts of your profile for a job posting and writes a cover letter.
4. **Render:** Go fills a LaTeX template with the result.
5. **Build:** pdflatex turns it into a PDF.

The AI only works with data, never LaTeX, and it can't change facts like job titles or dates. Everything it writes is checked before it's used.

## Project layout

- `extract/`: the Python code (extraction, profiles, tailoring) and the AI instructions in `prompts/`
- `renderer/`: the Go build tool (in progress)
- `render.go`: the current Go renderer
- `jake.tex.tmpl`: the LaTeX template
- `resume.schema.json`, `profile.schema.json`: the data formats
- `Dockerfile`: LaTeX and Go, for building PDFs

## Quick start

From the `extract` folder, with an Anthropic API key in `extract/.env`:

```
py extract.py my-resume.pdf resume.json
py make_profile.py resume.json profile.json
py tailor.py profile.json job.txt tailored.json
```

Then, from the main folder:

```
go run render.go jake.tex.tmpl tailored.json tailored.tex
docker run --rm -v "${PWD}:/work" resume-latex pdflatex tailored.tex
```

## Status

Done: extraction, profiles, tailoring, the template.
In progress: the Go build tool.
Planned: fitting to one page, chat edits, a web app.

## Credits

The template is based on [Jake's Resume](https://github.com/jakegut/resume) by Jake Gutierrez (MIT License).
