package fileops

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestEditCommitRequiresExistingTarget(t *testing.T) {
	parent := t.TempDir()
	if err := AtomicReplace(parent, filepath.Join(parent, "missing"), "new", false); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing target was accepted: %v", err)
	}
	entries, err := os.ReadDir(parent)
	if err != nil || len(entries) != 0 {
		t.Fatalf("created file for missing edit target: %v, %v", entries, err)
	}
}
