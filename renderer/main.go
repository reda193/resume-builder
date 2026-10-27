package main

import (
	"context"
	"encoding/json"
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
)

type Report struct {
	Worked   bool     `json:"ok"`
	PdfPath  string   `json:"pdf"`
	Pages    int      `json:"pages"`
	Problems []string `json:"problems"`
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: go run . <template> <data.json> <output.tex>")
		os.Exit(1)
	}

	tmplPath, dataPath, outPath := os.Args[1], os.Args[2], os.Args[3]

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

	charProblems := badCharacters(result, "")
	if len(charProblems) > 0 {
		report := Report{
			Worked:   false,
			PdfPath:  outPath,
			Pages:    0,
			Problems: charProblems,
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
		fail("Error running pdflatex:", err)
	}

	cmd = exec.CommandContext(ctx, "pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "resume.tex")
	cmd.Dir = dir
	output, err = cmd.CombinedOutput()
	if err != nil {
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
	fmt.Fprintln(os.Stderr, string(output))

	logData, err := os.ReadFile(filepath.Join(dir, "resume.log"))

	pagesPattern := regexp.MustCompile(`Output written on .* \((\d+) pages?`)

	match := pagesPattern.FindStringSubmatch(string(logData))
	if match == nil {
		fail("This is not a page number.", err)
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

var texEscaper = strings.NewReplacer(
	`\`, `\textbackslash{}`, // starts a command
	`&`, `\&`, // next table column
	`%`, `\%`, // comment: hides the rest of the line
	`$`, `\$`, // math mode
	`#`, `\#`, // command parameter
	`_`, `\_`, // subscript
	`{`, `\{`, // start group
	`}`, `\}`, // end group
	`~`, `\textasciitilde{}`, // non-breaking space
	`^`, `\textasciicircum{}`, // superscript
	`<`, `\textless{}`, // wrong symbol in old fonts
	`>`, `\textgreater{}`, // wrong symbol in old fonts
	`|`, `\textbar{}`, // wrong symbol in old fonts
)

var hrefEscaper = strings.NewReplacer(
	`%`, `\%`,
	`#`, `\#`,
	`&`, `\&`,
)

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

func myTex(s string) string {
	var b strings.Builder
	for _, ch := range s {
		switch ch {
		case '&':
			b.WriteString(`\&`)
		default:
			b.WriteRune(ch)
		}

	}
	return b.String()
}

func fail(msg string, err error) {
	fmt.Fprintln(os.Stderr, msg, err)
	os.Exit(1)
}

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
