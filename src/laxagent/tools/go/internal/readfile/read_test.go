package readfile

import (
	"strings"
	"testing"
)

func args() Args {
	return Args{Path: "/user/project/1/text.txt", StartLineNo: 1, StartByteNo: 1, TotalLines: 2000}
}

func TestUnicodeContinuation(t *testing.T) {
	text := strings.Repeat("中文🙂", 10000) + "\n最后一行"
	request := args()
	var joined strings.Builder
	pages := 0
	for {
		result, err := Read(strings.NewReader(text), request)
		if err != nil {
			t.Fatal(err)
		}
		if len(result.Text) == 0 || len(result.Text) > MaxBytes {
			t.Fatalf("invalid page length: %d", len(result.Text))
		}
		joined.WriteString(result.Text)
		pages++
		if result.EOF {
			if result.LastLineNo != 2 || result.LastByteNo != len("最后一行") {
				t.Fatalf("wrong final cursor: %+v", result)
			}
			break
		}
		request.StartLineNo, request.StartByteNo = result.LastLineNo, result.LastByteNo+1
		if result.LastLineComplete {
			request.StartLineNo++
			request.StartByteNo = 1
		}
	}
	if pages < 2 || joined.String() != text {
		t.Fatal("pagination lost or duplicated bytes")
	}
}

func TestPageBoundaries(t *testing.T) {
	tests := []struct {
		name, text        string
		line, byte, lines int
		want              Result
	}{
		{"empty", "", 1, 1, 1, Result{EOF: true, LastLineComplete: true}},
		{"crlf", "中a\r\n\n尾", 1, 4, 1, Result{"a\r\n", false, 1, true, 6}},
		{"empty-line", "中a\r\n\n尾", 2, 1, 1, Result{"\n", false, 2, true, 1}},
		{"last-line", "中a\r\n\n尾", 3, 1, 1, Result{"尾", true, 3, true, 3}},
		{"exact-limit", strings.Repeat("a", MaxBytes), 1, 1, 1, Result{strings.Repeat("a", MaxBytes), true, 1, true, MaxBytes}},
		{"split-after-newline", strings.Repeat("a", MaxBytes-2) + "\n中", 1, 1, 2, Result{strings.Repeat("a", MaxBytes-2) + "\n", false, 1, true, MaxBytes - 1}},
		{"line-cap", strings.Repeat("x\n", 2001), 1, 1, 5000, Result{strings.Repeat("x\n", 2000), false, 2000, true, 2}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			request := args()
			request.StartLineNo, request.StartByteNo, request.TotalLines = tt.line, tt.byte, tt.lines
			got, err := Read(strings.NewReader(tt.text), request)
			if err != nil || got != tt.want {
				t.Fatalf("got %+v, %v; want %+v", got, err, tt.want)
			}
		})
	}
}

func TestInvalidCursorAndText(t *testing.T) {
	for _, cursor := range [][2]int{{1, 2}, {1, 20}, {2, 1}} {
		request := args()
		request.StartLineNo, request.StartByteNo = cursor[0], cursor[1]
		_, err := Read(strings.NewReader("中文\n"), request)
		if err == nil || err.Error() != "INVALID_POSITION" {
			t.Fatalf("cursor %v: %v", cursor, err)
		}
	}
	for _, raw := range []string{"a\x00b", "\xff", "\xe4\xb8", "abc\xe4\n"} {
		_, err := Read(strings.NewReader(raw), args())
		if err == nil || err.Error() != "NOT_UTF8_TEXT" {
			t.Fatalf("text %q: %v", raw, err)
		}
	}
}

func TestAbsolutePaths(t *testing.T) {
	for _, name := range []string{"text.txt", "../secret", "/user/../secret", "/", "/user/", "/user/.", "/user/a\x00b"} {
		request := args()
		request.Path = name
		if err := Validate(request); err == nil || err.Error() != "READ_FILE_INVALID_PATH" {
			t.Fatalf("invalid path %q: %v", name, err)
		}
	}
	for _, name := range []string{"/user/project/2/text.txt", "/user/skills/example/SKILL.md", "/usr/share/text"} {
		request := args()
		request.Path = name
		if err := Validate(request); err != nil {
			t.Fatalf("valid path %q: %v", name, err)
		}
	}
}
