"""Fail closed on macOS ACL grants before retaining private profile data.

Mode bits omit extended ACL grants. Allow no extended allow entries, including
owner grants; deny-only ACLs add no rights. Query a non-following fd.
"""
import ctypes
import errno
import os
import sys


def require_private_parent(path, *, api=None, platform_name=None, directory=True):
    if (platform_name or sys.platform) != 'darwin':
        return
    api = ctypes.CDLL(None, use_errno=True) if api is None else api
    signatures = {
        'acl_get_fd_np': (ctypes.c_void_p, [ctypes.c_int, ctypes.c_int]),
        'acl_get_entry': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p)]),
        'acl_get_tag_type': (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]),
        'acl_free': (ctypes.c_int, [ctypes.c_void_p]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    acl = None
    try:
        ctypes.set_errno(0)
        acl = api.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED, Darwin SDK.
        if not acl:
            # An existing open fd plus ENOENT means FILESEC_ACL is absent.
            if ctypes.get_errno() != errno.ENOENT:
                raise PermissionError('Claude profile access could not be verified.')
        else:
            entry, tag = ctypes.c_void_p(), ctypes.c_int()
            for index in range(170):
                ctypes.set_errno(0)
                result = api.acl_get_entry(acl, 0 if index == 0 else -1, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    break  # Darwin ends this native ACL iterator with EINVAL.
                if result != 0 or api.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or tag.value != 2:
                    raise PermissionError('Claude profile access is shared or could not be verified.')
            else:
                raise PermissionError('Claude profile access could not be verified.')
        before, after = os.fstat(fd), path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise PermissionError('Claude profile access changed. Try again.')
    finally:
        if acl:
            api.acl_free(acl)
        os.close(fd)
