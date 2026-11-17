package writefile

import (
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"

	"laxagent.local/tools/internal/fileops"
)

func TestRunCreatesParentsAndDeletesMissingPaths(t *testing.T) {
	root := t.TempDir()
	request := writeArgs("中文\r\n")
	request.Path = filepath.Join(root, "new", "nested", "file.txt")
	if _, err := Run(request); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(request.Path)
	if err != nil || string(data) != *request.Content {
		t.Fatalf("read back: %q, %v", data, err)
	}
	info, err := os.Stat(filepath.Dir(request.Path))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("parent mode: %v, %v", info, err)
	}
	missing := Args{Operation: "delete", Path: filepath.Join(root, "missing", "file.txt")}
	result, err := Run(missing)
	if err != nil || result["deleted"] != false {
		t.Fatalf("missing parent delete: %v, %v", result, err)
	}
	if _, err := os.Stat(filepath.Dir(missing.Path)); !os.IsNotExist(err) {
		t.Fatalf("delete created parent: %v", err)
	}
}

func TestParentDescriptorStaysPinnedAcrossRename(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	parent := filepath.Join(root, "parent")
	if err := os.Mkdir(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	request := writeArgs("safe")
	request.Path = filepath.Join(parent, "file.txt")
	fd, err := fileops.OpenParent(request.Path, true)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(fd)
	saved := filepath.Join(root, "saved")
	if err := os.Rename(parent, saved); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, parent); err != nil {
		t.Fatal(err)
	}
	// Run must reject the replacement symlink before creating files outside.
	if _, err := Run(request); err == nil || err.Error() != "WRITE_FILE_INVALID_PATH" {
		t.Fatalf("accepted parent symlink: %v", err)
	}
	// The already opened descriptor still refers to the original directory.
	if _, err := Apply(filepath.Join("/proc/self/fd", strconv.Itoa(fd)), request); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(saved, "file.txt")); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(outside)
	if err != nil || len(entries) != 0 {
		t.Fatalf("wrote through symlink: %v, %v", entries, err)
	}
}
