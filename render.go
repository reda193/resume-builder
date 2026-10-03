package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"text/template"
)

func main() {
	jsonFile, err := os.Open("jake-ryan.json")

	if err != nil {
		fmt.Println(err)
		return
	}
	fmt.Println("Succesfully Opened json")

	defer jsonFile.Close()

	byteValue, err := io.ReadAll(jsonFile)
	if err != nil {
		fmt.Println("Error reading file:", err)
		return
	}

	var result map[string]any
	err = json.Unmarshal([]byte(byteValue), &result)
	if err != nil {
		fmt.Println("Error unmarshalling JSON:", err)
		return
	}

	basics := result["basics"].(map[string]any)
	fmt.Println(basics["name"])
	tmpl, err := template.New("test").
		Delims("<<", ">>").
		Funcs(template.FuncMap{"tex": tex}).
		Parse("Hello <<tex .basics.name>>, from <<tex \"R&D\">>\n")
	err = tmpl.Execute(os.Stdout, result)
	if err != nil {
		fmt.Println("Error filling template:", err)
		return
	}
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

func tex(s string) string {
	return texEscaper.Replace(s)
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
