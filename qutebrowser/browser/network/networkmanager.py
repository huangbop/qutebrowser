# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2015 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Our own QNetworkAccessManager."""

from PyQt5.QtCore import pyqtSlot, pyqtSignal, PYQT_VERSION, QCoreApplication
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkReply

try:
    from PyQt5.QtNetwork import QSslSocket
except ImportError:
    SSL_AVAILABLE = False
else:
    SSL_AVAILABLE = QSslSocket.supportsSsl()

from qutebrowser.config import config
from qutebrowser.utils import message, log, usertypes, utils, objreg
from qutebrowser.browser import cookies
from qutebrowser.browser.network import qutescheme, networkreply


HOSTBLOCK_ERROR_STRING = '%HOSTBLOCK%'


class NetworkManager(QNetworkAccessManager):

    """Our own QNetworkAccessManager.

    Attributes:
        _requests: Pending requests.
        _scheme_handlers: A dictionary (scheme -> handler) of supported custom
                          schemes.
        _win_id: The window ID this NetworkManager is associated with.
        _tab_id: The tab ID this NetworkManager is associated with.

    Signals:
        shutting_down: Emitted when the QNAM is shutting down.
    """

    shutting_down = pyqtSignal()

    def __init__(self, win_id, tab_id, parent=None):
        log.init.debug("Initializing NetworkManager")
        with log.disable_qt_msghandler():
            # WORKAROUND for a hang when a message is printed - See:
            # http://www.riverbankcomputing.com/pipermail/pyqt/2014-November/035045.html
            super().__init__(parent)
        log.init.debug("NetworkManager init done")
        self._win_id = win_id
        self._tab_id = tab_id
        self._requests = []
        self._scheme_handlers = {
            'qute': qutescheme.QuteSchemeHandler(win_id),
        }
        self._set_cookiejar()
        self._set_cache()
        if SSL_AVAILABLE:
            self.sslErrors.connect(self.on_ssl_errors)
        self.authenticationRequired.connect(self.on_authentication_required)
        self.proxyAuthenticationRequired.connect(
            self.on_proxy_authentication_required)
        objreg.get('config').changed.connect(self.on_config_changed)

    def _set_cookiejar(self, private=False):
        """Set the cookie jar of the NetworkManager correctly.

        Args:
            private: Whether we're currently in private browsing mode.
        """
        if private:
            cookie_jar = cookies.RAMCookieJar(self)
            self.setCookieJar(cookie_jar)
        else:
            # We have a shared cookie jar - we restore its parent so we don't
            # take ownership of it.
            app = QCoreApplication.instance()
            cookie_jar = objreg.get('cookie-jar')
            self.setCookieJar(cookie_jar)
            cookie_jar.setParent(app)

    def _set_cache(self):
        """Set the cache of the NetworkManager correctly.

        We can't switch the whole cache in private mode because QNAM would
        delete the old cache.
        """
        # We have a shared cache - we restore its parent so we don't take
        # ownership of it.
        app = QCoreApplication.instance()
        cache = objreg.get('cache')
        self.setCache(cache)
        cache.setParent(app)

    def _ask(self, text, mode, owner=None):
        """Ask a blocking question in the statusbar.

        Args:
            text: The text to display to the user.
            mode: A PromptMode.
            owner: An object which will abort the question if destroyed, or
                   None.

        Return:
            The answer the user gave or None if the prompt was cancelled.
        """
        q = usertypes.Question()
        q.text = text
        q.mode = mode
        self.shutting_down.connect(q.abort)
        if owner is not None:
            owner.destroyed.connect(q.abort)
        webview = objreg.get('webview', scope='tab', window=self._win_id,
                             tab=self._tab_id)
        webview.loadStarted.connect(q.abort)
        bridge = objreg.get('message-bridge', scope='window',
                            window=self._win_id)
        bridge.ask(q, blocking=True)
        q.deleteLater()
        return q.answer

    def _fill_authenticator(self, authenticator, answer):
        """Fill a given QAuthenticator object with an answer."""
        if answer is not None:
            # Since the answer could be something else than (user, password)
            # pylint seems to think we're unpacking a non-sequence. However we
            # *did* explicitely ask for a tuple, so it *will* always be one.
            user, password = answer
            authenticator.setUser(user)
            authenticator.setPassword(password)

    def shutdown(self):
        """Abort all running requests."""
        self.setNetworkAccessible(QNetworkAccessManager.NotAccessible)
        for request in self._requests:
            request.abort()
            request.deleteLater()
        self.shutting_down.emit()

    if SSL_AVAILABLE:
        @pyqtSlot('QNetworkReply*', 'QList<QSslError>')
        def on_ssl_errors(self, reply, errors):
            """Decide if SSL errors should be ignored or not.

            This slot is called on SSL/TLS errors by the self.sslErrors signal.

            Args:
                reply: The QNetworkReply that is encountering the errors.
                errors: A list of errors.
            """
            ssl_strict = config.get('network', 'ssl-strict')
            if ssl_strict == 'ask':
                err_string = '\n'.join('- ' + err.errorString() for err in
                                       errors)
                answer = self._ask('SSL errors - continue?\n{}'.format(
                    err_string), mode=usertypes.PromptMode.yesno, owner=reply)
                if answer:
                    reply.ignoreSslErrors()
            elif ssl_strict:
                pass
            else:
                for err in errors:
                    # FIXME we might want to use warn here (non-fatal error)
                    # https://github.com/The-Compiler/qutebrowser/issues/114
                    message.error(self._win_id,
                                  'SSL error: {}'.format(err.errorString()))
                reply.ignoreSslErrors()

    @pyqtSlot('QNetworkReply', 'QAuthenticator')
    def on_authentication_required(self, reply, authenticator):
        """Called when a website needs authentication."""
        answer = self._ask("Username ({}):".format(authenticator.realm()),
                           mode=usertypes.PromptMode.user_pwd,
                           owner=reply)
        self._fill_authenticator(authenticator, answer)

    @pyqtSlot('QNetworkProxy', 'QAuthenticator')
    def on_proxy_authentication_required(self, _proxy, authenticator):
        """Called when a proxy needs authentication."""
        answer = self._ask("Proxy username ({}):".format(
            authenticator.realm()), mode=usertypes.PromptMode.user_pwd)
        self._fill_authenticator(authenticator, answer)

    @config.change_filter('general', 'private-browsing')
    def on_config_changed(self):
        """Set cookie jar when entering/leaving private browsing mode."""
        private_browsing = config.get('general', 'private-browsing')
        if private_browsing:
            # switched from normal mode to private mode
            self._set_cookiejar(private=True)
        else:
            # switched from private mode to normal mode
            self._set_cookiejar()

    # WORKAROUND for:
    # http://www.riverbankcomputing.com/pipermail/pyqt/2014-September/034806.html
    #
    # By returning False, we provoke a TypeError because of a wrong return
    # type, which does *not* trigger a segfault but invoke our return handler
    # immediately.
    @utils.prevent_exceptions(False)
    def createRequest(self, op, req, outgoing_data):
        """Return a new QNetworkReply object.

        Extend QNetworkAccessManager::createRequest to save requests in
        self._requests and handle custom schemes.

        Args:
             op: Operation op
             req: const QNetworkRequest & req
             outgoing_data: QIODevice * outgoingData

        Return:
            A QNetworkReply.
        """
        scheme = req.url().scheme()
        if scheme == 'https' and not SSL_AVAILABLE:
            return networkreply.ErrorNetworkReply(
                req, "SSL is not supported by the installed Qt library!",
                QNetworkReply.ProtocolUnknownError, self)
        elif scheme in self._scheme_handlers:
            return self._scheme_handlers[scheme].createRequest(
                op, req, outgoing_data)
        if (op == QNetworkAccessManager.GetOperation and
                req.url().host() in objreg.get('host-blocker').blocked_hosts):
            log.webview.info("Request to {} blocked by host blocker.".format(
                req.url().host()))
            return networkreply.ErrorNetworkReply(
                req, HOSTBLOCK_ERROR_STRING, QNetworkReply.ContentAccessDenied,
                self)
        if config.get('network', 'do-not-track'):
            dnt = '1'.encode('ascii')
        else:
            dnt = '0'.encode('ascii')
        req.setRawHeader('DNT'.encode('ascii'), dnt)
        req.setRawHeader('X-Do-Not-Track'.encode('ascii'), dnt)
        accept_language = config.get('network', 'accept-language')
        if accept_language is not None:
            req.setRawHeader('Accept-Language'.encode('ascii'),
                             accept_language.encode('ascii'))
        if PYQT_VERSION < 0x050301:
            # WORKAROUND (remove this when we bump the requirements to 5.3.1)
            #
            # If we don't disable our message handler, we get a freeze if a
            # warning is printed due to a PyQt bug, e.g. when clicking a
            # currency on http://ch.mouser.com/localsites/
            #
            # See http://www.riverbankcomputing.com/pipermail/pyqt/2014-June/034420.html
            with log.disable_qt_msghandler():
                reply = super().createRequest(op, req, outgoing_data)
        else:
            reply = super().createRequest(op, req, outgoing_data)
        self._requests.append(reply)
        reply.destroyed.connect(lambda obj: self._requests.remove(obj))
        return reply
