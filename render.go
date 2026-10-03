package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"text/template"
)

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: go run render.go <template> <data.json> <output.tex>")
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

	out, err := os.Create(outPath)
	if err != nil {
		fail("Error creating output file:", err)
	}
	defer out.Close()

	err = tmpl.Execute(out, result)
	if err != nil {
		fail("Error filling template:", err)
	}

	fmt.Println("Wrote:", outPath)
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
