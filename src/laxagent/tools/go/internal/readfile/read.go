// Package readfile implements the read_file UTF-8 byte-cursor protocol.
package readfile

import (
	"bufio"
	"errors"
	"io"
	"path"
	"strings"
	"unicode/utf8"
)

const MaxBytes = 50 * 1024
const MaxLines = 2000

type Args struct {
	Path        string `json:"path"`
	StartLineNo int    `json:"start_line_no"`
	StartByteNo int    `json:"start_byte_no"`
	TotalLines  int    `json:"total_lines"`
}

type Result struct {
	Text             string `json:"text"`
	EOF              bool   `json:"eof"`
	LastLineNo       int    `json:"last_line_no"`
	LastLineComplete bool   `json:"last_line_complete"`
	LastByteNo       int    `json:"last_byte_no"`
}

func Validate(args Args) error {
	if args.StartLineNo < 1 || args.StartByteNo < 1 || args.TotalLines < 1 {
		return errors.New("INVALID_ARGUMENTS")
	}
	if !path.IsAbs(args.Path) || path.Clean(args.Path) == "/" ||
		strings.ContainsRune(args.Path, 0) || !utf8.ValidString(args.Path) ||
		strings.HasSuffix(args.Path, "/") || strings.HasSuffix(args.Path, "/.") {
		return errors.New("READ_FILE_INVALID_PATH")
	}
	for _, part := range strings.Split(args.Path, "/") {
		if part == ".." {
			return errors.New("READ_FILE_INVALID_PATH")
		}
	}
	return nil
}

// Read consumes at most one page after seeking to a one-based line/byte cursor.
// Cursor offsets count original bytes, including CRLF, never decoded characters.
// Run validates args before opening the file and calling Read.
func Read(input io.Reader, args Args) (Result, error) {
	result := Result{}
	r := bufio.NewReader(input)
	for line := 1; line < args.StartLineNo; line++ {
		for {
			_, err := r.ReadSlice('\n')
			if err == bufio.ErrBufferFull {
				continue
			}
			if err != nil {
				return result, cursorError(err)
			}
			break
		}
	}
	for offset := 1; offset < args.StartByteNo; offset++ {
		b, err := r.ReadByte()
		if err != nil {
			return result, cursorError(err)
		}
		if b == '\n' {
			return result, errors.New("INVALID_POSITION")
		}
	}
	first, err := r.Peek(1)
	if err == io.EOF {
		if args.StartLineNo != 1 || args.StartByteNo != 1 {
			return result, errors.New("INVALID_POSITION")
		}
		return Result{EOF: true, LastLineComplete: true}, nil
	}
	if err != nil {
		return result, errors.New("READ_FAILED")
	}
	if first[0]&0xc0 == 0x80 {
		return result, errors.New("INVALID_POSITION")
	}
	data := make([]byte, 0, MaxBytes)
	lines := 0
	for len(data) < MaxBytes && lines < min(args.TotalLines, MaxLines) {
		b, err := r.ReadByte()
		if err == io.EOF {
			break
		}
		if err != nil {
			return result, errors.New("READ_FAILED")
		}
		if b == 0 {
			return result, errors.New("NOT_UTF8_TEXT")
		}
		data = append(data, b)
		if b == '\n' {
			lines++
		}
	}
	_, err = r.Peek(1)
	if err != nil && err != io.EOF {
		return result, errors.New("READ_FAILED")
	}
	eof := err == io.EOF
	for offset := 0; offset < len(data); {
		runeValue, size := utf8.DecodeRune(data[offset:])
		if runeValue == utf8.RuneError && size == 1 {
			if !eof && !utf8.FullRune(data[offset:]) {
				data = data[:offset] // Resume from these unread UTF-8 bytes next call.
				break
			}
			return result, errors.New("NOT_UTF8_TEXT")
		}
		offset += size
	}
	result.LastLineNo = args.StartLineNo
	result.LastByteNo = args.StartByteNo - 1
	for _, b := range data {
		if result.LastLineComplete {
			result.LastLineNo++
			result.LastByteNo = 0
		}
		result.LastByteNo++
		result.LastLineComplete = b == '\n'
	}
	result.Text = string(data)
	result.EOF = eof
	result.LastLineComplete = result.LastLineComplete || eof
	return result, nil
}

func cursorError(err error) error {
	if err == io.EOF {
		return errors.New("INVALID_POSITION")
	}
	return errors.New("READ_FAILED")
}
