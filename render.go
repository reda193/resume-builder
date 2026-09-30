package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
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
	tmpl, err := template.New("test").Delims("<<", ">>").Parse("Hello <<.basics.name>>\n")
	err = tmpl.Execute(os.Stdout, result)
}
