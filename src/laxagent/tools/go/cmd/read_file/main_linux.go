package main

import (
	"encoding/json"
	"io"
	"os"

	"laxagent.local/tools/internal/readfile"
)

func main() {
	decoder := json.NewDecoder(os.Stdin)
	decoder.DisallowUnknownFields()
	var args readfile.Args
	response := map[string]any{"error_code": nil, "result": map[string]any{}}
	if err := decoder.Decode(&args); err != nil {
		response["error_code"] = "INVALID_ARGUMENTS"
	} else if err := decoder.Decode(new(any)); err != io.EOF {
		response["error_code"] = "INVALID_ARGUMENTS"
	} else if result, err := readfile.Run(args); err != nil {
		response["error_code"] = err.Error()
	} else {
		response["result"] = result
	}
	if err := json.NewEncoder(os.Stdout).Encode(response); err != nil {
		os.Exit(1)
	}
}
