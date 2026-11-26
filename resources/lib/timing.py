"""Compatibility helpers; production builds do not emit benchmark traces."""
import xbmc

def mark(message):
    pass

def span(name):
    return lambda fn: fn

def execute(request):
    return xbmc.executeJSONRPC(request)
