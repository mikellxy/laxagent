package fileops

import (
	"errors"
	"path"
	"strings"
	"syscall"
)

// OpenParent pins the parent without following user-controlled symlinks.
func OpenParent(filename string, create bool) (int, error) {
	fd, err := syscall.Open("/", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	parts := strings.Split(strings.TrimPrefix(path.Clean(filename), "/"), "/")
	for _, part := range parts[:len(parts)-1] {
		next, err := syscall.Openat(fd, part, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		if create && errors.Is(err, syscall.ENOENT) {
			err = syscall.Mkdirat(fd, part, 0o700)
			if err != nil && !errors.Is(err, syscall.EEXIST) {
				syscall.Close(fd)
				return -1, err
			}
			next, err = syscall.Openat(fd, part, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		}
		syscall.Close(fd)
		if err != nil {
			return -1, err
		}
		fd = next
	}
	return fd, nil
}
