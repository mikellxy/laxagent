package readfile

import (
	"errors"
	"os"
	"path"
	"strings"
	"syscall"
)

// Run opens from the sandbox root using directory descriptors. No component is
// followed through a symlink, including one swapped during path traversal.
func Run(args Args) (Result, error) {
	if err := Validate(args); err != nil {
		return Result{}, err
	}
	fd, err := syscall.Open("/", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return Result{}, fileError(err)
	}
	defer func() { syscall.Close(fd) }()
	parts := strings.Split(strings.TrimPrefix(path.Clean(args.Path), "/"), "/")
	for _, part := range parts[:len(parts)-1] {
		next, err := syscall.Openat(fd, part, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		if err != nil {
			return Result{}, fileError(err)
		}
		syscall.Close(fd)
		fd = next
	}
	fileFD, err := syscall.Openat(fd, parts[len(parts)-1], syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return Result{}, fileError(err)
	}
	file := os.NewFile(uintptr(fileFD), args.Path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return Result{}, fileError(err)
	}
	if !info.Mode().IsRegular() {
		return Result{}, errors.New("NOT_A_FILE")
	}
	return Read(file, args)
}

func fileError(err error) error {
	switch {
	case errors.Is(err, syscall.ENOENT):
		return errors.New("FILE_NOT_FOUND")
	case errors.Is(err, syscall.EACCES), errors.Is(err, syscall.EPERM):
		return errors.New("ACCESS_DENIED")
	case errors.Is(err, syscall.ELOOP), errors.Is(err, syscall.ENOTDIR), errors.Is(err, syscall.ENAMETOOLONG):
		return errors.New("READ_FILE_INVALID_PATH")
	default:
		return errors.New("READ_FAILED")
	}
}
