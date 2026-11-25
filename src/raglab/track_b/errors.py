"""Stable errors for the Track B parsing boundary."""


class TrackBError(Exception):
    code = "TRACK_B_ERROR"


class InvalidPackage(TrackBError):
    code = "TRACK_B_INVALID_PACKAGE"


class PackageLimitExceeded(TrackBError):
    code = "TRACK_B_PACKAGE_LIMIT"


class ForbiddenPart(TrackBError):
    code = "TRACK_B_FORBIDDEN_PART"


class UnsafeRelationship(TrackBError):
    code = "TRACK_B_UNSAFE_RELATIONSHIP"
