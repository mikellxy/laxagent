// Package writefile implements absolute-path UTF-8 writes and file deletion.
package writefile

import (
	"errors"
	"os"
	"path"
	"path/filepath"
	"strings"
	"syscall"
	"unicode/utf8"

	"laxagent.local/tools/internal/fileops"
)

type Args struct {
	Operation string  `json:"operation"`
	Path      string  `json:"path"`
	Content   *string `json:"content"`
}

type Error = fileops.Error

// Validate checks the executable's input once, before any filesystem changes.
func Validate(args Args) error {
	if (args.Operation != "write" && args.Operation != "delete") ||
		(args.Operation == "write" && args.Content == nil) ||
		(args.Operation == "delete" && args.Content != nil) {
		return Error("INVALID_ARGUMENTS")
	}
	if err := fileops.ValidatePath(args.Path); err != nil {
		return fileError(err)
	}
	if args.Content != nil && (!utf8.ValidString(*args.Content) || strings.ContainsRune(*args.Content, 0)) {
		return Error("NOT_UTF8_TEXT")
	}
	return nil
}

// Apply operates in a parent directory already opened and pinned by Run.
// It does not revalidate arguments or attach any special meaning to SKILL.md.
func Apply(parent string, args Args) (map[string]any, error) {
	filename := path.Clean(args.Path)
	target := filepath.Join(parent, path.Base(filename))
	if args.Operation == "delete" {
		_, err := fileops.RegularMode(target)
		if errors.Is(err, os.ErrNotExist) {
			return map[string]any{"path": filename, "deleted": false}, nil
		}
		if err != nil {
			return nil, fileError(err)
		}
		// Unlike os.Remove, unlink cannot remove an empty directory swapped in
		// after the regular-file check. It also never follows the leaf symlink.
		err = syscall.Unlink(target)
		if errors.Is(err, os.ErrNotExist) {
			return map[string]any{"path": filename, "deleted": false}, nil
		}
		if err != nil {
			return nil, fileError(err)
		}
		return map[string]any{"path": filename, "deleted": true}, nil
	}
	if err := fileops.AtomicReplace(parent, target, *args.Content, true); err != nil {
		return nil, fileError(err)
	}
	return map[string]any{"path": filename, "bytes_written": len(*args.Content)}, nil
}

func fileError(err error) error {
	return fileops.MapError(err, "WRITE_FILE_INVALID_PATH", "WRITE_FAILED")
}
