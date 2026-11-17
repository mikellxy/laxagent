// Package fileops contains filesystem primitives shared by sandbox file tools.
package fileops

import (
	"errors"
	"io"
	"os"
	"path"
	"strings"
	"syscall"
	"unicode/utf8"
)

type Error string

func (e Error) Error() string { return string(e) }

func ValidatePath(filename string) error {
	if !path.IsAbs(filename) || path.Clean(filename) == "/" ||
		strings.ContainsRune(filename, 0) || !utf8.ValidString(filename) ||
		strings.HasSuffix(filename, "/") || strings.HasSuffix(filename, "/.") {
		return Error("INVALID_PATH")
	}
	for _, part := range strings.Split(filename, "/") {
		if part == ".." {
			return Error("INVALID_PATH")
		}
	}
	return nil
}

func RegularMode(filename string) (os.FileMode, error) {
	info, err := os.Lstat(filename)
	if err != nil {
		return 0, err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return 0, Error("INVALID_PATH")
	}
	if !info.Mode().IsRegular() {
		return 0, Error("NOT_A_FILE")
	}
	return info.Mode().Perm(), nil
}

// ReadRegular operates beneath the already pinned parent. The leaf is checked
// again after opening because another call can change filesystem state.
func ReadRegular(filename string) ([]byte, error) {
	if _, err := RegularMode(filename); err != nil {
		return nil, err
	}
	fd, err := syscall.Open(filename, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), filename)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, Error("NOT_A_FILE")
	}
	return io.ReadAll(file)
}

func AtomicReplace(parent, target, content string, allowMissing bool) error {
	mode, err := RegularMode(target)
	if allowMissing && errors.Is(err, os.ErrNotExist) {
		mode = 0o600
	} else if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(parent, ".laxagent-write-*.tmp")
	if err != nil {
		return err
	}
	defer temporary.Close()
	committed := false
	defer func() {
		if !committed {
			os.Remove(temporary.Name())
		}
	}()
	if _, err = io.Copy(temporary, strings.NewReader(content)); err != nil {
		return err
	}
	if err = temporary.Chmod(mode); err != nil {
		return err
	}
	if err = temporary.Sync(); err != nil {
		return err
	}
	if err = temporary.Close(); err != nil {
		return err
	}
	// The target is mutable filesystem state, not a trusted internal argument.
	if _, err = RegularMode(target); err != nil && !(allowMissing && errors.Is(err, os.ErrNotExist)) {
		return err
	}
	if err = os.Rename(temporary.Name(), target); err != nil {
		return err
	}
	committed = true
	return nil
}

func MapError(err error, invalidPath, fallback string) error {
	if code, ok := err.(Error); ok {
		if code == "INVALID_PATH" {
			return Error(invalidPath)
		}
		return code
	}
	switch {
	case errors.Is(err, syscall.EACCES), errors.Is(err, syscall.EPERM), errors.Is(err, syscall.EROFS):
		return Error("ACCESS_DENIED")
	case errors.Is(err, syscall.ELOOP), errors.Is(err, syscall.ENOTDIR), errors.Is(err, syscall.ENAMETOOLONG):
		return Error(invalidPath)
	case errors.Is(err, syscall.EISDIR):
		return Error("NOT_A_FILE")
	default:
		return Error(fallback)
	}
}
