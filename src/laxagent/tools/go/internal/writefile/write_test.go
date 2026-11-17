package writefile

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"laxagent.local/tools/internal/fileops"
)

func writeArgs(content string) Args {
	return Args{Operation: "write", Path: "/user/project/1/file.txt", Content: &content}
}

func TestCreateOverwriteEmptyAndPermissions(t *testing.T) {
	parent := t.TempDir()
	for index, content := range []string{strings.Repeat("中文🙂\r\n", 10000), "短\r\n尾", ""} {
		request := writeArgs(content)
		request.Path = "/user/project/1/./file.txt"
		result, err := Apply(parent, request)
		if err != nil || result["path"] != "/user/project/1/file.txt" || result["bytes_written"] != len(content) {
			t.Fatalf("write result: %v, %v", result, err)
		}
		filename := filepath.Join(parent, "file.txt")
		data, err := os.ReadFile(filename)
		if err != nil || string(data) != content {
			t.Fatalf("read back: %q, %v", data, err)
		}
		info, err := os.Stat(filename)
		if err != nil {
			t.Fatal(err)
		}
		want := os.FileMode(0o751)
		if index == 0 {
			want = 0o600
		}
		if info.Mode().Perm() != want {
			t.Fatalf("mode %v, want %v", info.Mode(), want)
		}
		if err := os.Chmod(filename, 0o751); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := os.ReadDir(parent)
	if err != nil || len(entries) != 1 || entries[0].Name() != "file.txt" {
		t.Fatalf("temporary files left behind: %v, %v", entries, err)
	}
}

func TestInputValidation(t *testing.T) {
	for _, filename := range []string{"relative", "/", "/user/../file", "/user/", "/user/.", "/user/a\x00b"} {
		request := writeArgs("text")
		request.Path = filename
		if err := Validate(request); err == nil || err.Error() != "WRITE_FILE_INVALID_PATH" {
			t.Fatalf("path %q: %v", filename, err)
		}
	}
	for _, text := range []string{"a\x00b", "\xff"} {
		if err := Validate(writeArgs(text)); err == nil || err.Error() != "NOT_UTF8_TEXT" {
			t.Fatalf("content %q: %v", text, err)
		}
	}
	for _, request := range []Args{
		{Operation: "write", Path: "/user/file"},
		{Operation: "other", Path: "/user/file"},
		{Operation: "delete", Path: "/user/file", Content: writeArgs("text").Content},
	} {
		if err := Validate(request); err == nil || err.Error() != "INVALID_ARGUMENTS" {
			t.Fatalf("invalid args: %v", err)
		}
	}
}

func TestOrdinarySkillEntryAndIdempotentDelete(t *testing.T) {
	parent := t.TempDir()
	request := writeArgs("ordinary text, no skill metadata")
	request.Path = "/user/skills/example/SKILL.md"
	if err := Validate(request); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(parent, request); err != nil {
		t.Fatal(err)
	}
	request.Operation, request.Content = "delete", nil
	for _, deleted := range []bool{true, false} {
		result, err := Apply(parent, request)
		if err != nil || result["deleted"] != deleted || result["path"] != request.Path {
			t.Fatalf("delete result: %v, %v", result, err)
		}
	}
}

func TestSymlinksAndSpecialFilesAreNotModified(t *testing.T) {
	parent, outside := t.TempDir(), t.TempDir()
	secret := filepath.Join(outside, "secret")
	if err := os.WriteFile(secret, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(parent, "link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "missing"), filepath.Join(parent, "dangling")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(parent, "directory"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(parent, "fifo"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, operation := range []string{"write", "delete"} {
		for name, code := range map[string]string{"link": "WRITE_FILE_INVALID_PATH", "dangling": "WRITE_FILE_INVALID_PATH", "directory": "NOT_A_FILE", "fifo": "NOT_A_FILE"} {
			request := writeArgs("must not write")
			request.Path, request.Operation = "/user/project/1/"+name, operation
			if operation == "delete" {
				request.Content = nil
			}
			if _, err := Apply(parent, request); err == nil || err.Error() != code {
				t.Fatalf("%s %s: %v", operation, name, err)
			}
		}
	}
	data, err := os.ReadFile(secret)
	if err != nil || string(data) != "secret" {
		t.Fatalf("outside file changed: %q, %v", data, err)
	}
	entries, err := os.ReadDir(parent)
	if err != nil || len(entries) != 4 {
		t.Fatalf("unexpected files after failure: %v, %v", entries, err)
	}
}

func TestAtomicReplaceDoesNotModifyHardLinkSource(t *testing.T) {
	parent := t.TempDir()
	source := filepath.Join(t.TempDir(), "original")
	if err := os.WriteFile(source, []byte("original"), 0o751); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(source, filepath.Join(parent, "file.txt")); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(parent, writeArgs("replacement")); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(source)
	if err != nil || string(data) != "original" {
		t.Fatalf("hard link source changed: %q, %v", data, err)
	}
}

func TestWriteFailurePreservesOriginal(t *testing.T) {
	parent := t.TempDir()
	target := filepath.Join(parent, "file.txt")
	if err := os.WriteFile(target, []byte("original"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A missing destination directory forces rename failure after the temporary
	// file has been written and synced, exercising cleanup after partial progress.
	if err := fileops.AtomicReplace(parent, filepath.Join(parent, "missing", "file.txt"), "replacement", true); err == nil {
		t.Fatal("expected rename failure")
	}
	data, err := os.ReadFile(target)
	if err != nil || string(data) != "original" {
		t.Fatalf("original changed: %q, %v", data, err)
	}
	entries, err := os.ReadDir(parent)
	if err != nil || len(entries) != 1 {
		t.Fatalf("temporary file was not cleaned up: %v, %v", entries, err)
	}
}
