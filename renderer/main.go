package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"text/template"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"golang.org/x/text/language"
	"golang.org/x/text/message"
)

type Report struct {
	Worked   bool     `json:"ok"`
	PdfPath  string   `json:"pdf"`
	Pages    int      `json:"pages"`
	Problems []string `json:"problems"`
}

func main() {
	if len(os.Args) != 5 {
		fmt.Fprintln(os.Stderr, "usage: go run . <template> <schema.json> <data.json> <output.pdf>")
		os.Exit(2)
	}

	tmplPath, schemaPath, dataPath, outPath := os.Args[1], os.Args[2], os.Args[3], os.Args[4]
	report := build(tmplPath, schemaPath, dataPath, outPath)

	jsonData, err := json.MarshalIndent(report, "", "   ")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error marshaling JSON:", err)
		os.Exit(1)
	}
	fmt.Println(string(jsonData))

	if !report.Worked {
		os.Exit(1)
	}
}

func build(tmplPath, schemaPath, dataPath, outPath string) Report {
	report := Report{
		Worked:   false,
		PdfPath:  outPath,
		Pages:    0,
		Problems: []string{},
	}

	fail := func(what string, err error) Report {
		report.Problems = append(report.Problems, what+": "+err.Error())
		return report
	}

	compiler := jsonschema.NewCompiler()
	schema, err := compiler.Compile(schemaPath)
	if err != nil {
		return fail("could not load the schema", err)
	}

	jsonFile, err := os.Open(dataPath)
	if err != nil {
		return fail("could not open the data file", err)
	}
	defer jsonFile.Close()

	byteValue, err := io.ReadAll(jsonFile)
	if err != nil {
		return fail("could not read the data file", err)
	}

	var result map[string]any
	err = json.Unmarshal(byteValue, &result)
	if err != nil {
		return fail("the data file is not valid JSON", err)
	}

	report.Problems = append(report.Problems, schemaProblems(schema, result)...)
	report.Problems = append(report.Problems, badCharacters(result, "")...)
	if len(report.Problems) > 0 {
		return report
	}

	tmpl, err := template.New(filepath.Base(tmplPath)).
		Delims("<<", ">>").
		Funcs(template.FuncMap{
			"tex":      tex,
			"href":     href,
			"linktext": linktext,
		}).
		Option("missingkey=zero").
		ParseFiles(tmplPath)
	if err != nil {
		return fail("could not read the template", err)
	}

	dir, err := os.MkdirTemp("", "resume-build-*")
	if err != nil {
		return fail("could not create a temporary folder", err)
	}
	defer os.RemoveAll(dir)

	texPath := filepath.Join(dir, "resume.tex")
	out, err := os.Create(texPath)
	if err != nil {
		return fail("could not create the .tex file", err)
	}
	defer out.Close()

	err = tmpl.Execute(out, result)
	if err != nil {
		return fail("could not fill the template", err)
	}
	out.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	for pass := 1; pass <= 2; pass++ {
		output, err := runPdflatex(ctx, dir)
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return fail("pdflatex", errors.New("took longer than 30 seconds and was stopped"))
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, string(output))
			return fail("pdflatex failed", errors.New(latexError(output)))
		}
	}

	pdf, err := os.ReadFile(filepath.Join(dir, "resume.pdf"))
	if err != nil {
		return fail("could not read the PDF", err)
	}
	err = os.WriteFile(outPath, pdf, 0644)
	if err != nil {
		return fail("could not write the PDF", err)
	}

	logData, err := os.ReadFile(filepath.Join(dir, "resume.log"))
	if err != nil {
		return fail("could not read the pdflatex log", err)
	}

	pagesPattern := regexp.MustCompile(`Output written on .* \((\d+) pages?`)
	match := pagesPattern.FindStringSubmatch(string(logData))
	if match == nil {
		return fail("could not read the page count", errors.New("no page count in the log"))
	}
	pages, err := strconv.Atoi(match[1])
	if err != nil {
		return fail("could not read the page count", err)
	}
	report.Pages = pages

	overfullPattern := regexp.MustCompile(`Overfull \\hbox \(([\d.]+)pt too wide\)`)
	for _, m := range overfullPattern.FindAllStringSubmatch(string(logData), -1) {
		width, err := strconv.ParseFloat(m[1], 64)
		if err != nil {
			continue
		}
		if width > 1 {
			report.Problems = append(report.Problems, fmt.Sprintf("text runs past the margin by %.1fpt", width))
		}
	}

	report.Worked = len(report.Problems) == 0
	fmt.Fprintln(os.Stderr, "Wrote:", outPath)
	return report
}

func runPdflatex(ctx context.Context, dir string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex")
	cmd.Dir = dir
	return cmd.CombinedOutput()
}

func latexError(output []byte) string {
	for _, line := range strings.Split(string(output), "\n") {
		if strings.HasPrefix(line, "!") {
			return strings.TrimSpace(strings.TrimPrefix(line, "!"))
		}
	}
	return "unknown error, see the pdflatex output above"
}

func schemaProblems(schema *jsonschema.Schema, data any) []string {
	err := schema.Validate(data)
	if err == nil {
		return []string{}
	}
	validationErr, ok := err.(*jsonschema.ValidationError)
	if !ok {
		return []string{err.Error()}
	}
	return leafProblems(validationErr)
}

func leafProblems(e *jsonschema.ValidationError) []string {
	if len(e.Causes) == 0 {
		where := "/" + strings.Join(e.InstanceLocation, "/")
		return []string{fmt.Sprintf("%s: %s", where, e.ErrorKind.LocalizedString(printer))}
	}
	problems := []string{}
	for _, cause := range e.Causes {
		problems = append(problems, leafProblems(cause)...)
	}
	return problems
}

var printer = message.NewPrinter(language.English)

func allowed(r rune) bool {
	return (r >= 0x20 && r <= 0x7E) ||
		(r >= 0xA0 && r <= 0xFF) ||
		(r >= 0x100 && r <= 0x17F) ||
		strings.ContainsRune("–—‘’“”•…", r)
}

func badCharacters(val any, path string) []string {
	switch v := val.(type) {
	case map[string]any:
		problems := []string{}
		for key, item := range v {
			problems = append(problems, badCharacters(item, path+"/"+key)...)
		}
		return problems
	case []any:
		problems := []string{}
		for i, item := range v {
			problems = append(problems, badCharacters(item, path+"/"+strconv.Itoa(i))...)
		}
		return problems
	case string:
		problems := []string{}
		for _, r := range v {
			if !allowed(r) {
				problems = append(problems, fmt.Sprintf("%s: the character %q can't be printed", path, r))
			}
		}
		return problems
	default:
		return []string{}
	}
}

var texEscaper = strings.NewReplacer(
	`\`, `\textbackslash{}`,
	`&`, `\&`,
	`%`, `\%`,
	`$`, `\$`,
	`#`, `\#`,
	`_`, `\_`,
	`{`, `\{`,
	`}`, `\}`,
	`~`, `\textasciitilde{}`,
	`^`, `\textasciicircum{}`,
	`<`, `\textless{}`,
	`>`, `\textgreater{}`,
	`|`, `\textbar{}`,
)

var hrefEscaper = strings.NewReplacer(`%`, `\%`, `#`, `\#`, `&`, `\&`)

func href(s any) string {
	return hrefEscaper.Replace(fmt.Sprint(s))
}

func linktext(v any) string {
	s := fmt.Sprint(v)
	s = strings.TrimPrefix(s, "https://")
	s = strings.TrimPrefix(s, "http://")
	s = strings.TrimPrefix(s, "www.")
	s = strings.TrimSuffix(s, "/")
	return s
}

func tex(s any) string {
	if s == nil {
		return ""
	}
	return texEscaper.Replace(fmt.Sprint(s))
}
