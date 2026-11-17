package editfile

import (
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"

	"laxagent.local/tools/internal/fileops"
)

func TestRunDoesNotCreateMissingFileOrParents(t *testing.T) {
	root := t.TempDir()
	for _, filename := range []string{"missing", "nested/missing"} {
		args := request(TextEdit{"old", "new"})
		args.Path = filepath.Join(root, filename)
		if _, err := Run(args); err == nil || err.Error() != "FILE_NOT_FOUND" {
			t.Fatalf("missing file: %v", err)
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		t.Fatalf("created missing paths: %v, %v", entries, err)
	}
}

func TestRunAndPinnedParent(t *testing.T) {
	root := t.TempDir()
	parent := filepath.Join(root, "parent")
	if err := os.Mkdir(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	filename := filepath.Join(parent, "file.txt")
	if err := os.WriteFile(filename, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	args := request(TextEdit{"old", "new"})
	args.Path = filename
	if _, err := Run(args); err != nil {
		t.Fatal(err)
	}
	assertContents(t, filename, []byte("new"))
	fd, err := fileops.OpenParent(filename, false)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(fd)
	saved := filepath.Join(root, "saved")
	if err := os.Rename(parent, saved); err != nil {
		t.Fatal(err)
	}
	outside, secret := seed(t, []byte("new"))
	if err := os.Symlink(outside, parent); err != nil {
		t.Fatal(err)
	}
	args.Edits = []TextEdit{{"new", "updated"}}
	if _, err := Run(args); err == nil || err.Error() != "EDIT_FILE_INVALID_PATH" {
		t.Fatalf("accepted parent symlink: %v", err)
	}
	if _, err := Apply("/proc/self/fd/"+strconv.Itoa(fd), args); err != nil {
		t.Fatal(err)
	}
	assertContents(t, filepath.Join(saved, "file.txt"), []byte("updated"))
	assertContents(t, secret, []byte("new"))
}
