'''
wopiutils.py

General low-level functions to support the WOPI server.

Main author: Giuseppe.LoPresti@cern.ch, CERN/IT-ST
'''

import sys
import os
import time
import traceback
import json
from enum import Enum
from random import choice
from string import ascii_lowercase
from base64 import b64encode, b64decode
from binascii import Error as B64Error
from urllib.parse import quote_plus as url_quote_plus
import http.client
import flask
import jwt
from werkzeug.utils import secure_filename
import core.commoniface as common

# this is the xattr key used for conflicts resolution on the remote storage
LASTSAVETIMEKEY = 'iop.wopi.lastwritetime'

# convenience references to global entities
st = None
srv = None
log = None
endpoints = {}


class ViewMode(Enum):
    '''File view mode: reference is `ViewMode` at
    https://github.com/cs3org/cs3apis/blob/master/cs3/app/provider/v1beta1/provider_api.proto
    '''
    # The file can be opened but not downloaded
    VIEW_ONLY = "VIEW_MODE_VIEW_ONLY"
    # The file can be downloaded
    READ_ONLY = "VIEW_MODE_READ_ONLY"
    # The file can be downloaded and updated
    READ_WRITE = "VIEW_MODE_READ_WRITE"


class JsonLogger:
    '''A wrapper class in front of a logger, based on the facade pattern'''
    def __init__(self, logger):
        '''Initialization'''
        self.logger = logger

    def __getattr__(self, name):
        '''Facade method'''
        def facade(*args, **kwargs):
            '''internal method returned by __getattr__ and wrapping the original one'''
            if not hasattr(self.logger, name):
                raise NotImplementedError
            if name in ['debug', 'info', 'warning', 'error', 'fatal']:
                # resolve the current module
                f = traceback.extract_stack()[-2].filename
                m = f[f.rfind('/') + 1:f.rfind('.')]
                if m == '__init__':
                    # take 'module' out of '/path/to/module/__init__.py'
                    f = f[:f.rfind('/')]
                    m = f[f.rfind('/') + 1:]
                try:
                    # as we use a `key="value" ...` format in all logs, we only have args[0]
                    payload = 'module="%s" %s ' % (m, args[0])
                    # now convert the payload to a dictionary assuming no `="` nor `" ` is present inside any key or value!
                    # the added trailing space matches the `" ` split, so we remove the last element of that list
                    payload = dict([tuple(kv.split('="')) for kv in payload.split('" ')[:-1]])
                    # then convert dict -> json -> str + strip `{` and `}`
                    payload = str(json.dumps(payload))[1:-1]
                except Exception:    # pylint: disable=broad-except
                    # if the above assumptions do not hold, just json-escape the original log
                    payload = '"module": "%s", "payload": "%s"' % (m, json.dumps(args[0]))
                args = (payload,)
            # pass-through facade
            return getattr(self.logger, name)(*args, **kwargs)
        return facade


def logGeneralExceptionAndReturn(ex, req):
    '''Convenience function to log a stack trace and return HTTP 500'''
    ex_type, ex_value, ex_traceback = sys.exc_info()
    log.critical('msg="Unexpected exception caught" exception="%s" type="%s" traceback="%s" client="%s" requestedUrl="%s"' %
                 (ex, ex_type, traceback.format_exception(ex_type, ex_value, ex_traceback), req.remote_addr, req.url))
    return 'Internal error, please contact support', http.client.INTERNAL_SERVER_ERROR


def logExpiredTokenAndReturn(ex, req):
    '''Convenience function to log about an expired JWT token and return HTTP 401'''
    log.info('msg="Expired or malformed token" client="%s" requestedUrl="%s" error="%s" token="%s"' %
             (req.remote_addr, req.base_url, ex, req.args['access_token']))
    return 'Invalid access token', http.client.UNAUTHORIZED


def generateWopiSrc(fileid, proxy=False):
    '''Returns a URL-encoded WOPISrc for the given fileid, proxied if required.'''
    if not proxy or not srv.wopiproxy:
        return url_quote_plus('%s/wopi/files/%s' % (srv.wopiurl, fileid)).replace('-', '%2D')
    # proxy the WOPI request through an external WOPI proxy service, but only if it was not already proxied
    if len(fileid) < 50:   # heuristically, proxied fileids are (much) longer than that
        log.debug('msg="Generating proxied fileid" fileid="%s" proxy="%s"' % (fileid, srv.wopiproxy))
        fileid = jwt.encode({'u': srv.wopiurl + '/wopi/files/', 'f': fileid}, srv.wopiproxykey, algorithm='HS256')
    else:
        log.debug('msg="Proxied fileid already created" fileid="%s" proxy="%s"' % (fileid, srv.wopiproxy))
    return url_quote_plus('%s/wopi/files/%s' % (srv.wopiproxy, fileid)).replace('-', '%2D')


def getLibreOfficeLockName(filename):
    '''Returns the filename of a LibreOffice-compatible lock file.
    This enables interoperability between Online and Desktop applications'''
    return os.path.dirname(filename) + os.path.sep + '.~lock.' + os.path.basename(filename) + '#'


def getMicrosoftOfficeLockName(filename):
    '''Returns the filename of a lock file as created by Microsoft Office'''
    if os.path.splitext(filename)[1] != '.docx' or len(os.path.basename(filename)) <= 6 + 1 + 4:
        return os.path.dirname(filename) + os.path.sep + '~$' + os.path.basename(filename)
    # MS Word has a really weird algorithm for the lock file name...
    if len(os.path.basename(filename)) >= 8 + 1 + 4:
        return os.path.dirname(filename) + os.path.sep + '~$' + os.path.basename(filename)[2:]
    # elif len(os.path.basename(filename)) == 7+1+4:
    return os.path.dirname(filename) + os.path.sep + '~$' + os.path.basename(filename)[1:]


def randomString(size):
    '''One liner to get a random string of letters'''
    return ''.join([choice(ascii_lowercase) for _ in range(size)])


def generateAccessToken(userid, fileid, viewmode, user, folderurl, endpoint, app):
    '''Generates an access token for a given file and a given user, and returns a tuple with
    the file's inode and the URL-encoded access token.'''
    appname, appediturl, appviewurl = app
    username, wopiuser = user
    try:
        # stat the file to check for existence and get a version-invariant inode and modification time:
        # the inode serves as fileid (and must not change across save operations), the mtime is used for version information.
        statinfo = st.statx(endpoint, fileid, userid)
    except IOError as e:
        log.info('msg="Requested file not found or not a file" fileid="%s" error="%s"' % (fileid, e))
        raise
    exptime = int(time.time()) + srv.tokenvalidity
    if not appediturl:
        # deprecated: for backwards compatibility, work out the URLs from the discovered app endpoints
        fext = os.path.splitext(statinfo['filepath'])[1].lower()
        try:
            appediturl = endpoints[fext]['edit']
            appviewurl = endpoints[fext]['view']
        except KeyError:
            log.critical('msg="No app URLs registered for the given file type" fileext="%s" mimetypescount="%d"' %
                         (fext, len(endpoints) if endpoints else 0))
            raise IOError
    acctok = jwt.encode({'userid': userid, 'wopiuser': wopiuser, 'filename': statinfo['filepath'], 'username': username,
                         'viewmode': viewmode.value, 'folderurl': folderurl, 'endpoint': endpoint,
                         'appname': appname, 'appediturl': appediturl, 'appviewurl': appviewurl, 'exp': exptime},
                        srv.wopisecret, algorithm='HS256')
    log.info('msg="Access token generated" userid="%s" wopiuser="%s" mode="%s" endpoint="%s" filename="%s" inode="%s" '
             'mtime="%s" folderurl="%s" appname="%s" expiration="%d" token="%s"' %
             (userid[-20:], wopiuser if wopiuser != userid else username, viewmode, endpoint,
              statinfo['filepath'], statinfo['inode'], statinfo['mtime'],
              folderurl, appname, exptime, acctok[-20:]))
    # return the inode == fileid, the filepath and the access token
    return statinfo['inode'], acctok


def encodeLock(lock):
    '''Generates the lock payload for the storage given the raw metadata'''
    if lock:
        return common.WEBDAV_LOCK_PREFIX + ' ' + b64encode(lock.encode()).decode()
    return None


def _decodeLock(storedlock):
    '''Restores the lock payload reverting the `encodeLock` format. May raise IOError'''
    try:
        if storedlock and storedlock.find(common.WEBDAV_LOCK_PREFIX) == 0:
            return b64decode(storedlock[len(common.WEBDAV_LOCK_PREFIX) + 1:].encode()).decode()
        raise IOError('Non-WOPI lock found')     # it's not our lock, though it's likely valid
    except B64Error as e:
        raise IOError(e)


def retrieveWopiLock(fileid, operation, lockforlog, acctok, overridefn=None):
    '''Retrieves and logs a lock for a given file: returns the lock and its holder, or (None, None) if no lock found'''
    encacctok = flask.request.args['access_token'][-20:] if 'access_token' in flask.request.args else 'NA'

    # if required, check if a non-WOPI office lock exists for this file
    checkext = srv.config.get('general', 'detectexternallocks', fallback='True').upper() == 'TRUE'
    lolock = lolockstat = None
    if checkext and os.path.splitext(acctok['filename'])[1] not in srv.nonofficetypes:
        try:
            # first try to look for a MS Office lock
            mslockstat = st.stat(acctok['endpoint'], getMicrosoftOfficeLockName(acctok['filename']), acctok['userid'])
            log.info('msg="Found existing MS Office lock" lockop="%s" user="%s" filename="%s" token="%s" lockmtime="%ld"' %
                     (operation.title(), acctok['userid'][-20:], acctok['filename'], encacctok, mslockstat['mtime']))
            return 'External', 'Microsoft Office for Desktop'
        except IOError:
            pass
        try:
            # then try to read a LibreOffice lock
            lolockstat = st.statx(acctok['endpoint'], getLibreOfficeLockName(acctok['filename']), acctok['userid'], versioninv=0)
            lolock = next(st.readfile(acctok['endpoint'], getLibreOfficeLockName(acctok['filename']), acctok['userid'], None))
            if isinstance(lolock, IOError):
                # this might be an access error, optimistically move on
                raise lolock
            lolock = lolock.decode()
            if 'WOPIServer' not in lolock:
                lolockholder = lolock.split(',')[1] if ',' in lolock else lolockstat['ownerid']
                log.info('msg="Found existing LibreOffice lock" lockop="%s" user="%s" filename="%s" token="%s" '
                         'lockmtime="%ld" holder="%s"' %
                         (operation.title(), acctok['userid'][-20:], acctok['filename'], encacctok,
                          lolockstat['mtime'], lolockholder))
                return 'External', 'LibreOffice for Desktop'
        except (IOError, StopIteration):
            pass

    try:
        # fetch and decode the lock
        lockcontent = st.getlock(acctok['endpoint'], overridefn if overridefn else acctok['filename'], acctok['userid'])
        # here we used to check the last save time to "extend" the validity of an otherwise expired lock:
        # however, this goes against isolating the lock expiration logic in the storage interfaces and ultimately
        # violates the WOPI specifications, therefore it was dropped
        if not lockcontent:
            log.info('msg="No lock found" lockop="%s" user="%s" filename="%s" token="%s"' %
                     (operation.title(), acctok['userid'][-20:], acctok['filename'], encacctok))
            # lazily remove the LibreOffice-compatible lock file, if it was detected and has
            # the expected signature - cf. setLock()
            try:
                if lolock:
                    st.removefile(acctok['endpoint'], getLibreOfficeLockName(acctok['filename']), acctok['userid'], True)
            except IOError as e:
                log.warning('msg="Unable to delete stale LibreOffice-compatible lock file" lockop="%s" user="%s" filename="%s" '
                            'fileid="%s" error="%s"' %
                            (operation.title(), acctok['userid'][-20:], acctok['filename'], fileid, e))
            return None, None
        storedlock = lockcontent['lock_id']
        lockcontent['lock_id'] = _decodeLock(storedlock)
    except IOError as e:
        log.info('msg="Found non-compatible or unreadable lock" lockop="%s" user="%s" filename="%s" token="%s" error="%s"' %
                 (operation.title(), acctok['userid'][-20:], acctok['filename'], encacctok, e))
        return 'External', 'Another app or user'

    log.info('msg="Retrieved lock" lockop="%s" user="%s" filename="%s" fileid="%s" lock="%s" '
             'retrievedlock="%s" expTime="%s" token="%s"' %
             (operation.title(), acctok['userid'][-20:], acctok['filename'], fileid, lockforlog, lockcontent['lock_id'],
              time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(lockcontent['expiration']['seconds'])), encacctok))
    return lockcontent['lock_id'], lockcontent['app_name']


def compareWopiLocks(lock1, lock2):
    '''Compares two locks and returns True if they represent the same WOPI lock.
    Officially, the comparison must be based on the locks' string representations, but because of
    a bug in Word Online, currently the internal format of the WOPI locks is looked at, based
    on heuristics. Note that this format is subject to change and is not documented!'''
    if lock1 == lock2:
        log.debug('msg="compareLocks" lock1="%s" lock2="%s" result="True"' % (lock1, lock2))
        return True
    if srv.config.get('general', 'wopilockstrictcheck', fallback='False').upper() == 'TRUE':
        log.debug('msg="compareLocks" lock1="%s" lock2="%s" strict="True" result="False"' % (lock1, lock2))
        return False

    # before giving up, attempt to parse the lock as a JSON dictionary if allowed by the config
    try:
        l1 = json.loads(lock1)
        try:
            l2 = json.loads(lock2)
            if 'S' in l1 and 'S' in l2:
                log.debug('msg="compareLocks" lock1="%s" lock2="%s" strict="False" result="%r"' %
                          (lock1, lock2, l1['S'] == l2['S']))
                return l1['S'] == l2['S']         # used by Word
            log.debug('msg="compareLocks" lock1="%s" lock2="%s" strict="False" result="False"' % (lock1, lock2))
            return False
        except (TypeError, ValueError):
            # lock2 is not a JSON dictionary
            if 'S' in l1:
                log.debug('msg="compareLocks" lock1="%s" lock2="%s" strict="False" result="%r"' %
                          (lock1, lock2, l1['S'] == lock2))
                return l1['S'] == lock2                    # also used by Word (BUG!)
    except (TypeError, ValueError):
        # lock1 is not a JSON dictionary: log the lock values and fail the comparison
        log.debug('msg="compareLocks" lock1="%s" lock2="%s" strict="False" result="False"' % (lock1, lock2))
        return False


def forceSmartUnlock(operation, retrievedlock, lockholder, lock, acctok):
    '''Handle the case when the retrieved lock is different from the given one but a "smart unlock" could be executed'''
    if srv.config.get('general', 'wopismartunlock', fallback='False').upper() == 'FALSE':
        return False
    if lockholder != acctok['appname']:
        # smart-unlock only applies if the retrieved lock and the current one belong to the same app
        log.info('msg="Found another lock holder" lockop="%s" filename="%s" appname="%s" token="%s" lock="%s"' %
                 (operation.title(), acctok['filename'], acctok['appname'], flask.request.args['access_token'][-20:], lock))
        return False
    try:
        savetime = st.getxattr(acctok['endpoint'], acctok['filename'], acctok['userid'], LASTSAVETIMEKEY)
        if not savetime or not savetime.isdigit():
            # attribute not found, play safe and assume the session is active
            savetime = time.time()
        if int(savetime) < time.time() - srv.config.getint("general", "wopilockexpiration"):
            # don't execute the unlock-relock yet for the time being
            #st.unlock(acctok['endpoint'], acctok['filename'], acctok['userid'], lockholder, encodeLock(retrievedlock))
            #st.setlock(acctok['endpoint'], acctok['filename'], acctok['userid'], lockholder, encodeLock(lock))
            log.warning('msg="Lock might be forced" lockop="%s" filename="%s" token="%s" lock="%s"' %
                        (operation.title(), acctok['filename'], flask.request.args['access_token'][-20:], lock))
            return True
        log.info('msg="Found another active session" lockop="%s" filename="%s" savetime="%d" token="%s" lock="%s"' %
                 (operation.title(), acctok['filename'], int(savetime), flask.request.args['access_token'][-20:], lock))
    except IOError as e:
        log.warning('msg="Failed attempt to force relock" lockop="%s" filename="%s" token="%s" lock="%s" error="%s"' %
                    (operation.title(), acctok['filename'], flask.request.args['access_token'][-20:], lock, e))
    return False


def makeConflictResponse(operation, retrievedlock, lock, oldlock, filename, reason=None):
    '''Generates and logs an HTTP 409 response in case of locks conflict'''
    resp = flask.Response(mimetype='application/json')
    resp.headers['X-WOPI-Lock'] = retrievedlock if retrievedlock else ''
    resp.status_code = http.client.CONFLICT
    if reason:
        # this is either a simple message or a dictionary: in all cases we want a dictionary to be JSON-ified
        if type(reason) == str:
            reason = {'message': reason}
        resp.headers['X-WOPI-LockFailureReason'] = reason['message']
        resp.data = json.dumps(reason)
    log.warning('msg="Returning conflict" lockop="%s" filename="%s" token="%s" lock="%s" '
                'oldlock="%s" retrievedlock="%s" reason="%s"' %
                (operation.title(), filename, flask.request.args['access_token'][-20:],
                 lock, oldlock, retrievedlock, (reason['message'] if reason else 'NA')))
    return resp


def storeWopiFile(request, retrievedlock, acctok, xakey, targetname=''):
    '''Saves a file from an HTTP request to the given target filename (defaulting to the access token's one),
         and stores the save time as an xattr. Throws IOError in case of any failure'''
    if not targetname:
        targetname = acctok['filename']
    st.writefile(acctok['endpoint'], targetname, acctok['userid'], request.get_data(), encodeLock(retrievedlock))
    # save the current time for later conflict checking: this is never older than the mtime of the file
    st.setxattr(acctok['endpoint'], targetname, acctok['userid'], xakey, int(time.time()), encodeLock(retrievedlock))


def getConflictPath(username):
    '''Returns the path to a suitable conflict path directory for a given user'''
    return srv.conflictpath.replace('user_initial', username[0]).replace('username', username)


def storeForRecovery(content, username, filename, acctokforlog, exception):
    try:
        filepath = srv.recoverypath + os.sep + time.strftime('%Y%m%dT%H%M%S') + '_editedby_' + username \
                   + '_origat_' + secure_filename(filename)
        with open(filepath, mode='wb') as f:
            written = f.write(content)
        if written != len(content):
            raise IOError('Size mismatch')
        log.error('msg="Error writing file, a copy was stored locally for later recovery" '
                  + 'filename="%s" recoveredpath="%s" token="%s" error="%s"' %
                  (filename, filepath, acctokforlog, exception))
    except (OSError, IOError) as e:
        log.critical('msg="Error writing file and failed to recover it to local storage, data is LOST" '
                     + 'filename="%s" token="%s" originalerror="%s" recoveryerror="%s"' %
                     (filename, acctokforlog, exception, e))
