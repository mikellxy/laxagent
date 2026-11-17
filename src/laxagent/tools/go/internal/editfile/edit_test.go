package editfile

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

//go:embed testdata/matching.json
var matchingCases []byte

func request(edits ...TextEdit) Args {
	return Args{Path: "/user/project/1/file.txt", Edits: edits}
}

func seed(t *testing.T, content []byte) (string, string) {
	t.Helper()
	parent := t.TempDir()
	filename := filepath.Join(parent, "file.txt")
	if err := os.WriteFile(filename, content, 0o600); err != nil {
		t.Fatal(err)
	}
	return parent, filename
}

func assertContents(t *testing.T, filename string, expected []byte) {
	t.Helper()
	data, err := os.ReadFile(filename)
	if err != nil || string(data) != string(expected) {
		t.Fatalf("file = %q, %v; want %q", data, err, expected)
	}
}

func TestMatchingCompatibility(t *testing.T) {
	// Golden cases migrated from the original Python edit_file unit tests.
	var cases []struct {
		Original string
		Edits    []TextEdit
		Expected string
		Error    string
	}
	if err := json.Unmarshal(matchingCases, &cases); err != nil {
		t.Fatal(err)
	}
	for index, test := range cases {
		t.Run(fmt.Sprint(index), func(t *testing.T) {
			parent, filename := seed(t, []byte(test.Original))
			before, err := os.Stat(filename)
			if err != nil {
				t.Fatal(err)
			}
			result, err := Apply(parent, request(test.Edits...))
			if test.Error != "" {
				if err == nil || err.Error() != test.Error {
					t.Fatalf("error = %v, want %s", err, test.Error)
				}
				assertContents(t, filename, []byte(test.Original))
				after, err := os.Stat(filename)
				if err != nil || !os.SameFile(before, after) {
					t.Fatal("failed edit replaced original")
				}
			} else {
				if err != nil || result["edits_applied"] != len(test.Edits) || result["bytes_written"] != len(test.Expected) {
					t.Fatalf("result = %v, %v", result, err)
				}
				assertContents(t, filename, []byte(test.Expected))
			}
			entries, err := os.ReadDir(parent)
			if err != nil || len(entries) != 1 {
				t.Fatalf("temporary files left behind: %v, %v", entries, err)
			}
		})
	}
}

func TestUnorderedAdjacentEditsUseOriginalOffsets(t *testing.T) {
	parent, filename := seed(t, []byte("中firstSECOND🙂last"))
	args := request(TextEdit{"last", "first"}, TextEdit{"first", "longer replacement"}, TextEdit{"SECOND", ""})
	args.Path = "/user/project/1/./file.txt"
	result, err := Apply(parent, args)
	expected := "中longer replacement🙂first"
	if err != nil || result["path"] != "/user/project/1/file.txt" || result["edits_applied"] != 3 || result["bytes_written"] != len(expected) {
		t.Fatalf("result: %v, %v", result, err)
	}
	assertContents(t, filename, []byte(expected))
}

func TestNoChangeDoesNotReplaceFile(t *testing.T) {
	parent, filename := seed(t, []byte("original"))
	before, err := os.Stat(filename)
	if err != nil {
		t.Fatal(err)
	}
	result, err := Apply(parent, request(TextEdit{"original", "original"}))
	if err != nil || result["bytes_written"] != 0 || result["edits_applied"] != 1 {
		t.Fatalf("result: %v, %v", result, err)
	}
	after, err := os.Stat(filename)
	if err != nil || !os.SameFile(before, after) || before.ModTime() != after.ModTime() {
		t.Fatal("no-op edit changed original")
	}
}

func TestInvalidInputAndFileText(t *testing.T) {
	for _, filename := range []string{"relative", "/", "/user/../file", "/user/", "/user/.", "/user/a\x00b"} {
		args := request(TextEdit{"old", "new"})
		args.Path = filename
		if err := Validate(args); err == nil || err.Error() != "EDIT_FILE_INVALID_PATH" {
			t.Fatalf("path %q: %v", filename, err)
		}
	}
	for _, args := range []Args{request(), request(TextEdit{"", "x"})} {
		if err := Validate(args); err == nil || err.Error() != "INVALID_ARGUMENTS" {
			t.Fatalf("invalid edits: %v", err)
		}
	}
	for _, edit := range []TextEdit{{"old", "a\x00b"}, {"old", "\xff"}, {"o\x00ld", "new"}} {
		if err := Validate(request(edit)); err == nil || err.Error() != "NOT_UTF8_TEXT" {
			t.Fatalf("invalid edit text: %v", err)
		}
	}
	for _, content := range []string{"old\xff", "old\x00", "old\xe4\xb8"} {
		parent, filename := seed(t, []byte(content))
		if _, err := Apply(parent, request(TextEdit{"old", "new"})); err == nil || err.Error() != "NOT_UTF8_TEXT" {
			t.Fatalf("invalid file text: %v", err)
		}
		assertContents(t, filename, []byte(content))
	}
}

func TestMissingSymlinkAndSpecialFiles(t *testing.T) {
	parent := t.TempDir()
	secret := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(secret, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(parent, "link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(parent, "missing"), filepath.Join(parent, "dangling")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(parent, "directory"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(parent, "fifo"), 0o600); err != nil {
		t.Fatal(err)
	}
	for filename, code := range map[string]string{"missing": "FILE_NOT_FOUND", "link": "EDIT_FILE_INVALID_PATH", "dangling": "EDIT_FILE_INVALID_PATH", "directory": "NOT_A_FILE", "fifo": "NOT_A_FILE"} {
		args := request(TextEdit{"old", "new"})
		args.Path = "/user/project/1/" + filename
		if _, err := Apply(parent, args); err == nil || err.Error() != code {
			t.Fatalf("%s: %v", filename, err)
		}
	}
	assertContents(t, secret, []byte("old"))
}

func TestSkillEntryIsOrdinaryTextAndHardLinkIsReplaced(t *testing.T) {
	parent := t.TempDir()
	_, original := seed(t, []byte("old"))
	if err := os.Chmod(original, 0o751); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(parent, "SKILL.md")
	if err := os.Link(original, target); err != nil {
		t.Fatal(err)
	}
	args := request(TextEdit{"old", "new"})
	args.Path = "/user/skills/example/SKILL.md"
	if err := Validate(args); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(parent, args); err != nil {
		t.Fatal(err)
	}
	assertContents(t, target, []byte("new"))
	assertContents(t, original, []byte("old"))
	info, err := os.Stat(target)
	if err != nil || info.Mode().Perm() != 0o751 {
		t.Fatalf("permissions not preserved: %v, %v", info, err)
	}
}
