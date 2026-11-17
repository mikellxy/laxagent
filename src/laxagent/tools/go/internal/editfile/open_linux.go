package editfile

import (
	"strconv"
	"syscall"

	"laxagent.local/tools/internal/fileops"
)

func Run(args Args) (map[string]any, error) {
	if err := Validate(args); err != nil {
		return nil, err
	}
	fd, err := fileops.OpenParent(args.Path, false)
	if err != nil {
		return nil, fileError(err, "READ_FAILED")
	}
	defer syscall.Close(fd)
	return Apply("/proc/self/fd/"+strconv.Itoa(fd), args)
}
