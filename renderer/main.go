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
		os.Exit(1)
	}

	tmplPath, schemaPath, dataPath, outPath := os.Args[1], os.Args[2], os.Args[3], os.Args[4]

	compiler := jsonschema.NewCompiler()
	schema, err := compiler.Compile(schemaPath)
	if err != nil {
		fail("Error loading schema:", err)
	}

	jsonFile, err := os.Open(dataPath)
	if err != nil {
		fail("Error opening file:", err)
	}
	defer jsonFile.Close()

	byteValue, err := io.ReadAll(jsonFile)
	if err != nil {
		fail("Error reading file:", err)
	}

	var result map[string]any
	err = json.Unmarshal(byteValue, &result)
	if err != nil {
		fail("Error decoding JSON:", err)
	}

	problems := schemaProblems(schema, result)
	problems = append(problems, badCharacters(result, "")...)
	if len(problems) > 0 {
		report := Report{
			Worked:   false,
			PdfPath:  outPath,
			Pages:    0,
			Problems: problems,
		}
		jsonData, _ := json.MarshalIndent(report, "", "   ")
		fmt.Println(string(jsonData))
		os.Exit(1)
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
		fail("Error reading template:", err)
	}

	dir, err := os.MkdirTemp("", "resume-build-*")
	if err != nil {
		fail("Error making directory:", err)
	}
	defer os.RemoveAll(dir)

	texPath := filepath.Join(dir, "resume.tex")
	out, err := os.Create(texPath)
	if err != nil {
		fail("Error creating output file:", err)
	}
	defer out.Close()

	err = tmpl.Execute(out, result)
	if err != nil {
		fail("Error filling template:", err)
	}
	out.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex")
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	if err != nil {
		fmt.Fprintln(os.Stderr, string(output))
		fail("Error running pdflatex:", err)
	}
	cmd = exec.CommandContext(ctx, "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex")
	cmd.Dir = dir
	output, err = cmd.CombinedOutput()
	if err != nil {
		fmt.Fprintln(os.Stderr, string(output))
		fail("Error running pdflatex:", err)
	}

	pdf, err := os.ReadFile(filepath.Join(dir, "resume.pdf"))
	if err != nil {
		fail("Error reading PDF:", err)
	}
	err = os.WriteFile(outPath, pdf, 0644)
	if err != nil {
		fail("Error writing PDF:", err)
	}

	logData, err := os.ReadFile(filepath.Join(dir, "resume.log"))
	if err != nil {
		fail("Error reading log:", err)
	}
	pagesPattern := regexp.MustCompile(`Output written on .* \((\d+) pages?`)
	match := pagesPattern.FindStringSubmatch(string(logData))
	if match == nil {
		fail("Error reading page count:", errors.New("no page count in the log"))
	}
	val, err := strconv.Atoi(match[1])
	if err != nil {
		fail("Conversion error:", err)
	}

	report := Report{
		Worked:   true,
		PdfPath:  outPath,
		Pages:    val,
		Problems: []string{},
	}

	overfullPattern := regexp.MustCompile(`Overfull \\hbox \(([\d.]+)pt too wide\)`)
	matches := overfullPattern.FindAllStringSubmatch(string(logData), -1)
	for _, m := range matches {
		width, err := strconv.ParseFloat(m[1], 64)
		if err != nil {
			continue
		}
		if width > 1 {
			report.Problems = append(report.Problems, fmt.Sprintf("text runs past the margin by %.1fpt", width))
		}
	}
	if len(report.Problems) > 0 {
		report.Worked = false
	}

	fmt.Fprintln(os.Stderr, "Wrote:", outPath)
	jsonData, err := json.MarshalIndent(report, "", "   ")
	if err != nil {
		fail("Error marshaling JSON:", err)
	}
	fmt.Println(string(jsonData))
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

func fail(msg string, err error) {
	fmt.Fprintln(os.Stderr, msg, err)
	os.Exit(1)
}
