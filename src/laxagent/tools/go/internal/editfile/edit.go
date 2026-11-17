// Package editfile applies an entire batch of edits against original UTF-8 bytes.
package editfile

import (
	"bytes"
	"errors"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"laxagent.local/tools/internal/fileops"
)

type TextEdit struct {
	OldText string `json:"old_text"`
	NewText string `json:"new_text"`
}

type Args struct {
	Path  string     `json:"path"`
	Edits []TextEdit `json:"edits"`
}

type Error = fileops.Error

func Validate(args Args) error {
	if err := fileops.ValidatePath(args.Path); err != nil {
		return fileError(err, "READ_FAILED")
	}
	if len(args.Edits) == 0 {
		return Error("INVALID_ARGUMENTS")
	}
	for _, edit := range args.Edits {
		if edit.OldText == "" {
			return Error("INVALID_ARGUMENTS")
		}
		for _, text := range []string{edit.OldText, edit.NewText} {
			if !utf8.ValidString(text) || strings.ContainsRune(text, 0) {
				return Error("NOT_UTF8_TEXT")
			}
		}
	}
	return nil
}

// Apply receives validated arguments and a parent directory pinned by Run.
func Apply(parent string, args Args) (map[string]any, error) {
	filename := path.Clean(args.Path)
	target := filepath.Join(parent, path.Base(filename))
	original, err := fileops.ReadRegular(target)
	if err != nil {
		return nil, fileError(err, "READ_FAILED")
	}
	updated, err := Transform(original, args.Edits)
	if err != nil {
		return nil, err
	}
	written := 0
	if !bytes.Equal(original, updated) {
		if err := fileops.AtomicReplace(parent, target, string(updated), false); err != nil {
			return nil, fileError(err, "WRITE_FAILED")
		}
		written = len(updated)
	}
	return map[string]any{"path": filename, "edits_applied": len(args.Edits), "bytes_written": written}, nil
}

type line struct {
	start, bodyEnd, end int
	key                 []byte
}

type match struct {
	start, end  int
	replacement string
}

func lines(content []byte) []line {
	var result []line
	for start := 0; start < len(content); {
		newline := bytes.IndexByte(content[start:], '\n')
		end := len(content)
		if newline != -1 {
			end = start + newline + 1
		}
		bodyEnd := end
		if newline != -1 {
			bodyEnd--
			if bodyEnd > start && content[bodyEnd-1] == '\r' {
				bodyEnd--
			}
		}
		result = append(result, line{start, bodyEnd, end, bytes.Trim(content[start:bodyEnd], " \t")})
		start = end
	}
	return result
}

func findRange(content, old []byte, indexed []line) (int, int, error) {
	start := bytes.Index(content, old)
	if start != -1 {
		// Start one byte later so overlapping occurrences also count.
		if bytes.Index(content[start+1:], old) != -1 {
			return 0, 0, Error("EDIT_AMBIGUOUS")
		}
		return start, start + len(old), nil
	}
	if strings.TrimFunc(string(old), func(r rune) bool {
		// Match Python str.isspace, including its four ASCII separators.
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	}) == "" {
		return 0, 0, Error("EDIT_NOT_FOUND")
	}
	wanted := lines(old)
	needsNewline := old[len(old)-1] == '\n'
	matchStart, matchEnd := -1, 0
	for index := 0; index <= len(indexed)-len(wanted); index++ {
		equal := true
		for offset, wantedLine := range wanted {
			if !bytes.Equal(indexed[index+offset].key, wantedLine.key) {
				equal = false
				break
			}
		}
		if !equal {
			continue
		}
		last := indexed[index+len(wanted)-1]
		if needsNewline && last.bodyEnd == last.end {
			continue
		}
		if matchStart != -1 {
			return 0, 0, Error("EDIT_AMBIGUOUS")
		}
		matchStart, matchEnd = indexed[index].start, last.bodyEnd
		if needsNewline {
			matchEnd = last.end
		}
	}
	if matchStart == -1 {
		return 0, 0, Error("EDIT_NOT_FOUND")
	}
	return matchStart, matchEnd, nil
}

// Transform checks file contents, then locates every edit against that same
// original buffer. Internal offsets need no revalidation during reconstruction.
func Transform(original []byte, edits []TextEdit) ([]byte, error) {
	if !utf8.Valid(original) || bytes.IndexByte(original, 0) != -1 {
		return nil, Error("NOT_UTF8_TEXT")
	}
	indexed := lines(original)
	matches := make([]match, 0, len(edits))
	for _, edit := range edits {
		start, end, err := findRange(original, []byte(edit.OldText), indexed)
		if err != nil {
			return nil, err
		}
		matches = append(matches, match{start, end, edit.NewText})
	}
	sort.Slice(matches, func(i, j int) bool { return matches[i].start < matches[j].start })
	for index := 1; index < len(matches); index++ {
		if matches[index-1].end > matches[index].start {
			return nil, Error("EDIT_OVERLAP")
		}
	}
	updated := make([]byte, 0, len(original))
	cursor := 0
	for _, item := range matches {
		updated = append(updated, original[cursor:item.start]...)
		updated = append(updated, item.replacement...)
		cursor = item.end
	}
	return append(updated, original[cursor:]...), nil
}

func fileError(err error, fallback string) error {
	if errors.Is(err, os.ErrNotExist) {
		return Error("FILE_NOT_FOUND")
	}
	return fileops.MapError(err, "EDIT_FILE_INVALID_PATH", fallback)
}
