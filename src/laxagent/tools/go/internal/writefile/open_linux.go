package writefile

import (
	"errors"
	"path"
	"strconv"
	"syscall"

	"laxagent.local/tools/internal/fileops"
)

func Run(args Args) (map[string]any, error) {
	if err := Validate(args); err != nil {
		return nil, err
	}
	fd, err := fileops.OpenParent(args.Path, args.Operation == "write")
	if err != nil {
		if args.Operation == "delete" && errors.Is(err, syscall.ENOENT) {
			return map[string]any{"path": path.Clean(args.Path), "deleted": false}, nil
		}
		return nil, fileError(err)
	}
	defer syscall.Close(fd)
	// bwrap mounts proc. This trusted descriptor path pins the parent directory
	// across concurrent renames; no user-supplied ancestor is followed again.
	return Apply("/proc/self/fd/"+strconv.Itoa(fd), args)
}
