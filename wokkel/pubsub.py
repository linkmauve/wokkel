# -*- test-case-name: wokkel.test.test_pubsub -*-
#
# Copyright (c) 2003-2007 Ralph Meijer
# See LICENSE for details.

"""
XMPP publish-subscribe protocol.

This protocol is specified in
U{XEP-0060<http://www.xmpp.org/extensions/xep-0060.html>}.
"""

from zope.interface import implements

from twisted.internet import defer
from twisted.words.protocols.jabber import jid, error, xmlstream
from twisted.words.xish import domish

from wokkel import disco, data_form
from wokkel.subprotocols import IQHandlerMixin, XMPPHandler
from wokkel.iwokkel import IPubSubClient, IPubSubService

# Iq get and set XPath queries
IQ_GET = '/iq[@type="get"]'
IQ_SET = '/iq[@type="set"]'

# Publish-subscribe namespaces
NS_PUBSUB = 'http://jabber.org/protocol/pubsub'
NS_PUBSUB_EVENT = NS_PUBSUB + '#event'
NS_PUBSUB_ERRORS = NS_PUBSUB + '#errors'
NS_PUBSUB_OWNER = NS_PUBSUB + "#owner"
NS_PUBSUB_NODE_CONFIG = NS_PUBSUB + "#node_config"
NS_PUBSUB_META_DATA = NS_PUBSUB + "#meta-data"

# In publish-subscribe namespace XPath query selector.
IN_NS_PUBSUB = '[@xmlns="' + NS_PUBSUB + '"]'
IN_NS_PUBSUB_OWNER = '[@xmlns="' + NS_PUBSUB_OWNER + '"]'

# Publish-subscribe XPath queries
PUBSUB_ELEMENT = '/pubsub' + IN_NS_PUBSUB
PUBSUB_OWNER_ELEMENT = '/pubsub' + IN_NS_PUBSUB_OWNER
PUBSUB_GET = IQ_GET + PUBSUB_ELEMENT
PUBSUB_SET = IQ_SET + PUBSUB_ELEMENT
PUBSUB_OWNER_GET = IQ_GET + PUBSUB_OWNER_ELEMENT
PUBSUB_OWNER_SET = IQ_SET + PUBSUB_OWNER_ELEMENT

# Publish-subscribe command XPath queries
PUBSUB_PUBLISH = PUBSUB_SET + '/publish' + IN_NS_PUBSUB
PUBSUB_CREATE = PUBSUB_SET + '/create' + IN_NS_PUBSUB
PUBSUB_SUBSCRIBE = PUBSUB_SET + '/subscribe' + IN_NS_PUBSUB
PUBSUB_UNSUBSCRIBE = PUBSUB_SET + '/unsubscribe' + IN_NS_PUBSUB
PUBSUB_OPTIONS_GET = PUBSUB_GET + '/options' + IN_NS_PUBSUB
PUBSUB_OPTIONS_SET = PUBSUB_SET + '/options' + IN_NS_PUBSUB
PUBSUB_DEFAULT = PUBSUB_OWNER_GET + '/default' + IN_NS_PUBSUB_OWNER
PUBSUB_CONFIGURE_GET = PUBSUB_OWNER_GET + '/configure' + IN_NS_PUBSUB_OWNER
PUBSUB_CONFIGURE_SET = PUBSUB_OWNER_SET + '/configure' + IN_NS_PUBSUB_OWNER
PUBSUB_SUBSCRIPTIONS = PUBSUB_GET + '/subscriptions' + IN_NS_PUBSUB
PUBSUB_AFFILIATIONS = PUBSUB_GET + '/affiliations' + IN_NS_PUBSUB
PUBSUB_AFFILIATIONS_GET = PUBSUB_OWNER_GET + '/affiliations' + \
                          IN_NS_PUBSUB_OWNER
PUBSUB_AFFILIATIONS_SET = PUBSUB_OWNER_SET + '/affiliations' + \
                          IN_NS_PUBSUB_OWNER
PUBSUB_SUBSCRIPTIONS_GET = PUBSUB_OWNER_GET + '/subscriptions' + \
                          IN_NS_PUBSUB_OWNER
PUBSUB_SUBSCRIPTIONS_SET = PUBSUB_OWNER_SET + '/subscriptions' + \
                          IN_NS_PUBSUB_OWNER
PUBSUB_ITEMS = PUBSUB_GET + '/items' + IN_NS_PUBSUB
PUBSUB_RETRACT = PUBSUB_SET + '/retract' + IN_NS_PUBSUB
PUBSUB_PURGE = PUBSUB_OWNER_SET + '/purge' + IN_NS_PUBSUB_OWNER
PUBSUB_DELETE = PUBSUB_OWNER_SET + '/delete' + IN_NS_PUBSUB_OWNER

class BadRequest(error.StanzaError):
    """
    Bad request stanza error.
    """
    def __init__(self):
        error.StanzaError.__init__(self, 'bad-request')



class SubscriptionPending(Exception):
    """
    Raised when the requested subscription is pending acceptance.
    """


class SubscriptionUnconfigured(Exception):
    """
    Raised when the requested subscription needs to be configured before
    becoming active.
    """


class PubSubError(error.StanzaError):
    """
    Exception with publish-subscribe specific condition.
    """
    def __init__(self, condition, pubsubCondition, feature=None, text=None):
        appCondition = domish.Element((NS_PUBSUB_ERRORS, pubsubCondition))
        if feature:
            appCondition['feature'] = feature
        error.StanzaError.__init__(self, condition,
                                         text=text,
                                         appCondition=appCondition)


class Unsupported(PubSubError):
    def __init__(self, feature, text=None):
        PubSubError.__init__(self, 'feature-not-implemented',
                                   'unsupported',
                                   feature,
                                   text)


class OptionsUnavailable(Unsupported):
    def __init__(self):
        Unsupported.__init__(self, 'subscription-options-unavailable')


class Item(domish.Element):
    """
    Publish subscribe item.

    This behaves like an object providing L{domish.IElement}.

    Item payload can be added using C{addChild} or C{addRawXml}, or using the
    C{payload} keyword argument to C{__init__}.
    """

    def __init__(self, id=None, payload=None):
        """
        @param id: optional item identifier
        @type id: L{unicode}
        @param payload: optional item payload. Either as a domish element, or
                        as serialized XML.
        @type payload: object providing L{domish.IElement} or L{unicode}.
        """

        domish.Element.__init__(self, (None, 'item'))
        if id is not None:
            self['id'] = id
        if payload is not None:
            if isinstance(payload, basestring):
                self.addRawXml(payload)
            else:
                self.addChild(payload)

class PubSubRequest(xmlstream.IQ):
    """
    Base class for publish subscribe user requests.

    @cvar namespace: request namespace
    @cvar verb: request verb
    @cvar method: type attribute of the IQ request. Either C{'set'} or C{'get'}
    @ivar command: command element of the request. This is the direct child of
                   the C{pubsub} element in the C{namespace} with the name
                   C{verb}.
    """

    namespace = NS_PUBSUB
    method = 'set'

    def __init__(self, xs):
        xmlstream.IQ.__init__(self, xs, self.method)
        self.addElement((self.namespace, 'pubsub'))

        self.command = self.pubsub.addElement(self.verb)

    def send(self, to):
        destination = unicode(to)
        return xmlstream.IQ.send(self, destination)

class CreateNode(PubSubRequest):
    verb = 'create'

    def __init__(self, xs, node=None):
        PubSubRequest.__init__(self, xs)
        if node:
            self.command["node"] = node

class DeleteNode(PubSubRequest):
    verb = 'delete'
    def __init__(self, xs, node):
        PubSubRequest.__init__(self, xs)
        self.command["node"] = node

class Subscribe(PubSubRequest):
    verb = 'subscribe'

    def __init__(self, xs, node, subscriber):
        PubSubRequest.__init__(self, xs)
        self.command["node"] = node
        self.command["jid"] = subscriber.full()

class Publish(PubSubRequest):
    verb = 'publish'

    def __init__(self, xs, node):
        PubSubRequest.__init__(self, xs)
        self.command["node"] = node

    def addItem(self, id=None, payload=None):
        item = self.command.addElement("item")
        item.addChild(payload)

        if id is not None:
            item["id"] = id

        return item

class PubSubClient(XMPPHandler):
    """
    Publish subscribe client protocol.
    """

    implements(IPubSubClient)

    def connectionInitialized(self):
        self.xmlstream.addObserver('/message/event[@xmlns="%s"]/items' %
                                   NS_PUBSUB_EVENT, self._onItems)

    def _onItems(self, message):
        try:
            notifier = jid.JID(message["from"])
            node = message.event.items["node"]
        except KeyError:
            return

        items = [element for element in message.event.items.elements()
                         if element.name == 'item']

        self.itemsReceived(notifier, node, items)

    def itemsReceived(self, notifier, node, items):
        pass

    def createNode(self, service, node=None):
        request = CreateNode(self.xmlstream, node)

        def cb(iq):
            try:
                new_node = iq.pubsub.create["node"]
            except AttributeError:
                # the suggested node identifier was accepted
                new_node = node
            return new_node

        return request.send(service).addCallback(cb)

    def deleteNode(self, service, node):
        return DeleteNode(self.xmlstream, node).send(service)

    def subscribe(self, service, node, subscriber):
        request = Subscribe(self.xmlstream, node, subscriber)

        def cb(iq):
            subscription = iq.pubsub.subscription["subscription"]

            if subscription == 'pending':
                raise SubscriptionPending
            elif subscription == 'unconfigured':
                raise SubscriptionUnconfigured
            else:
                # we assume subscription == 'subscribed'
                # any other value would be invalid, but that should have
                # yielded a stanza error.
                return None

        return request.send(service).addCallback(cb)

    def publish(self, service, node, items=[]):
        request = Publish(self.xmlstream, node)
        for item in items:
            request.command.addChild(item)

        return request.send(service)

class PubSubService(XMPPHandler, IQHandlerMixin):
    """
    Protocol implementation for a XMPP Publish Subscribe Service.

    The word Service here is used as taken from the Publish Subscribe
    specification. It is the party responsible for keeping nodes and their
    subscriptions, and sending out notifications.

    Methods from the L{IPubSubService} interface that are called as
    a result of an XMPP request may raise exceptions. Alternatively the
    deferred returned by these methods may have their errback called. These are
    handled as follows:

    * If the exception is an instance of L{error.StanzaError}, an error
      response iq is returned.
    * Any other exception is reported using L{log.msg}. An error response
      with the condition C{internal-server-error} is returned.

    The default implementation of said methods raises an L{Unsupported}
    exception and are meant to be overridden.

    @ivar discoIdentity: Service discovery identity as a dictionary with
                         keys C{'category'}, C{'type'} and C{'name'}.
    @ivar pubSubFeatures: List of supported publish-subscribe features for
                          service discovery, as C{str}.
    @type pubSubFeatures: C{list} or C{None}.
    """

    implements(IPubSubService)

    iqHandlers = {
            PUBSUB_PUBLISH: '_onPublish',
            PUBSUB_CREATE: '_onCreate',
            PUBSUB_SUBSCRIBE: '_onSubscribe',
            PUBSUB_OPTIONS_GET: '_onOptionsGet',
            PUBSUB_OPTIONS_SET: '_onOptionsSet',
            PUBSUB_AFFILIATIONS: '_onAffiliations',
            PUBSUB_ITEMS: '_onItems',
            PUBSUB_RETRACT: '_onRetract',
            PUBSUB_SUBSCRIPTIONS: '_onSubscriptions',
            PUBSUB_UNSUBSCRIBE: '_onUnsubscribe',

            PUBSUB_AFFILIATIONS_GET: '_onAffiliationsGet',
            PUBSUB_AFFILIATIONS_SET: '_onAffiliationsSet',
            PUBSUB_CONFIGURE_GET: '_onConfigureGet',
            PUBSUB_CONFIGURE_SET: '_onConfigureSet',
            PUBSUB_DEFAULT: '_onDefault',
            PUBSUB_PURGE: '_onPurge',
            PUBSUB_DELETE: '_onDelete',
            PUBSUB_SUBSCRIPTIONS_GET: '_onSubscriptionsGet',
            PUBSUB_SUBSCRIPTIONS_SET: '_onSubscriptionsSet',

            }

    def __init__(self):
        self.discoIdentity = {'category': 'pubsub',
                              'type': 'generic',
                              'name': 'Generic Publish-Subscribe Service'}

        self.pubSubFeatures = []

    def connectionMade(self):
        self.xmlstream.addObserver(PUBSUB_GET, self.handleRequest)
        self.xmlstream.addObserver(PUBSUB_SET, self.handleRequest)
        self.xmlstream.addObserver(PUBSUB_OWNER_GET, self.handleRequest)
        self.xmlstream.addObserver(PUBSUB_OWNER_SET, self.handleRequest)

    def getDiscoInfo(self, target, requestor, nodeIdentifier):
        info = []

        if not nodeIdentifier:
            info.append(disco.DiscoIdentity(**self.discoIdentity))

            info.append(disco.DiscoFeature(disco.NS_ITEMS))
            info.extend([disco.DiscoFeature("%s#%s" % (NS_PUBSUB, feature))
                         for feature in self.pubSubFeatures])

            return defer.succeed(info)
        else:
            def toInfo(nodeInfo):
                if not nodeInfo:
                    return []

                (nodeType, metaData) = nodeInfo
                info.append(disco.Identity('pubsub', nodeType))
                if metaData:
                    form = data_form.Form(type="result",
                                          form_type=NS_PUBSUB_META_DATA) 
                    form.add_field("text-single",
                                   "pubsub#node_type",
                                   "The type of node (collection or leaf)",
                                   nodeType)

                    for metaDatum in metaData:
                        form.add_field(**metaDatum)

                    info.append(form)
                return info

            d = self.getNodeInfo(requestor, nodeIdentifier)
            d.addCallback(toInfo)
            return d

    def getDiscoItems(self, target, requestor, nodeIdentifier):
        if nodeIdentifier or self.hideNodes:
            return defer.succeed([])

        d = self.getNodes(requestor)
        d.addCallback(lambda nodes: [disco.DiscoItem(target, node)
                                     for node in nodes])
        return d

    def _onPublish(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.publish["node"]
        except KeyError:
            raise BadRequest

        items = []
        for element in iq.pubsub.publish.elements():
            if element.uri == NS_PUBSUB and element.name == 'item':
                items.append(element)

        return self.publish(requestor, nodeIdentifier, items)

    def _onSubscribe(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.subscribe["node"]
            subscriber = jid.internJID(iq.pubsub.subscribe["jid"])
        except KeyError:
            raise BadRequest

        def toResponse(subscription):
            nodeIdentifier, state = subscription
            response = domish.Element((NS_PUBSUB, "pubsub"))
            subscription = response.addElement("subscription")
            subscription["node"] = nodeIdentifier
            subscription["jid"] = subscriber.full()
            subscription["subscription"] = state
            return response

        d = self.subscribe(requestor, nodeIdentifier, subscriber)
        d.addCallback(toResponse)
        return d

    def _onUnsubscribe(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.unsubscribe["node"]
            subscriber = jid.internJID(iq.pubsub.unsubscribe["jid"])
        except KeyError:
            raise BadRequest

        return self.unsubscribe(requestor, nodeIdentifier, subscriber)

    def _onOptionsGet(self, iq):
        raise Unsupported('subscription-options-unavailable')

    def _onOptionsSet(self, iq):
        raise Unsupported('subscription-options-unavailable')

    def _onSubscriptions(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            subscriptions = response.addElement('subscriptions')
            for node, subscriber, state in result:
                item = subscriptions.addElement('subscription')
                item['node'] = node
                item['jid'] = subscriber.full()
                item['subscription'] = state
            return response

        d = self.subscriptions(requestor)
        d.addCallback(toResponse)
        return d

    def _onAffiliations(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            affiliations = response.addElement('affiliations')

            for nodeIdentifier, affiliation in result:
                item = affiliations.addElement('affiliation')
                item['node'] = nodeIdentifier
                item['affiliation'] = affiliation

            return response

        d = self.affiliations(requestor)
        d.addCallback(toResponse)
        return d

    def _onCreate(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()
        nodeIdentifier = iq.pubsub.create.getAttribute("node")

        def toResponse(result):
            if not nodeIdentifier or nodeIdentifier != result:
                response = domish.Element((NS_PUBSUB, 'pubsub'))
                create = response.addElement('create')
                create['node'] = result
                return response
            else:
                return None

        d = self.create(requestor, nodeIdentifier)
        d.addCallback(toResponse)
        return d

    def _formFromConfiguration(self, options):
        form = data_form.Form(type="form", form_type=NS_PUBSUB_NODE_CONFIG)

        for option in options:
            form.add_field(**option)

        return form

    def _onDefault(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        def toResponse(options):
            response = domish.Element((NS_PUBSUB_OWNER, "pubsub"))
            default = response.addElement("default")
            default.addChild(self._formFromConfiguration(options))
            return response

        d = self.getDefaultConfiguration(requestor)
        d.addCallback(toResponse)
        return d

    def _onConfigureGet(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()
        nodeIdentifier = iq.pubsub.configure.getAttribute("node")

        def toResponse(options):
            response = domish.Element((NS_PUBSUB_OWNER, "pubsub"))
            configure = response.addElement("configure")
            configure.addChild(self._formFromConfiguration(options))

            if nodeIdentifier:
                configure["node"] = nodeIdentifier

            return response

        d = self.getConfiguration(requestor, nodeIdentifier)
        d.addCallback(toResponse)
        return d

    def _onConfigureSet(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()
        nodeIdentifier = iq.pubsub.configure["node"]

        def getFormOptions(self, form):
            options = {}

            for element in form.elements():
                if element.name == 'field' and \
                   element.uri == data_form.NS_X_DATA:
                    try:
                        options[element["var"]] = str(element.value)
                    except (KeyError, AttributeError):
                        raise BadRequest

            return options

        # Search configuration form with correct FORM_TYPE and process it

        for element in iq.pubsub.configure.elements():
            if element.name != 'x' or element.uri != data_form.NS_X_DATA:
                continue

            type = element.getAttribute("type")
            if type == "cancel":
                return None
            elif type != "submit":
                continue

            options = getFormOptions(element)

            if options["FORM_TYPE"] == NS_PUBSUB + "#node_config":
                del options["FORM_TYPE"]
                return self.setConfiguration(requestor, nodeIdentifier,
                                             options)

        raise BadRequest

    def _onItems(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.items["node"]
        except KeyError:
            raise BadRequest

        maxItems = iq.pubsub.items.getAttribute('max_items')

        if maxItems:
            try:
                maxItems = int(maxItems)
            except ValueError:
                raise BadRequest

        itemIdentifiers = []
        for child in iq.pubsub.items.elements():
            if child.name == 'item' and child.uri == NS_PUBSUB:
                try:
                    itemIdentifiers.append(child["id"])
                except KeyError:
                    raise BadRequest

        def toResponse(result):
            response = domish.Element((NS_PUBSUB, 'pubsub'))
            items = response.addElement('items')
            items["node"] = nodeIdentifier

            for item in result:
                items.addRawXml(item)

            return response

        d = self.items(requestor, nodeIdentifier, maxItems, itemIdentifiers)
        d.addCallback(toResponse)
        return d

    def _onRetract(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.retract["node"]
        except KeyError:
            raise BadRequest

        itemIdentifiers = []
        for child in iq.pubsub.retract.elements():
            if child.uri == NS_PUBSUB_OWNER and child.name == 'item':
                try:
                    itemIdentifiers.append(child["id"])
                except KeyError:
                    raise BadRequest

        return self.retract(requestor, nodeIdentifier, itemIdentifiers)

    def _onPurge(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.purge["node"]
        except KeyError:
            raise BadRequest

        return self.purge(requestor, nodeIdentifier)

    def _onDelete(self, iq):
        requestor = jid.internJID(iq["from"]).userhostJID()

        try:
            nodeIdentifier = iq.pubsub.delete["node"]
        except KeyError:
            raise BadRequest

        return self.delete(requestor, nodeIdentifier)

    def _onAffiliationsGet(self, iq):
        raise Unsupported('modify-affiliations')

    def _onAffiliationsSet(self, iq):
        raise Unsupported('modify-affiliations')

    def _onSubscriptionsGet(self, iq):
        raise Unsupported('manage-subscriptions')

    def _onSubscriptionsSet(self, iq):
        raise Unsupported('manage-subscriptions')

    # public methods

    def notifyPublish(self, entity, nodeIdentifier, notifications):

        print notifications
        for recipient, items in notifications:
            message = domish.Element((None, "message"))
            message["from"] = entity.full()
            message["to"] = recipient.full()
            event = message.addElement((NS_PUBSUB_EVENT, "event"))
            element = event.addElement("items")
            element["node"] = nodeIdentifier
            element.children = items
            self.send(message)

    def getNodeInfo(self, requestor, nodeIdentifier):
        return None

    def getNodes(self, requestor):
        return []

    def publish(self, requestor, nodeIdentifier, items):
        raise Unsupported('publish')

    def subscribe(self, requestor, nodeIdentifier, subscriber):
        raise Unsupported('subscribe')

    def unsubscribe(self, requestor, nodeIdentifier, subscriber):
        raise Unsupported('subscribe')

    def subscriptions(self, requestor):
        raise Unsupported('retrieve-subscriptions')

    def affiliations(self, requestor):
        raise Unsupported('retrieve-affiliations')

    def create(self, requestor, nodeIdentifier):
        raise Unsupported('create-nodes')

    def getDefaultConfiguration(self, requestor):
        raise Unsupported('retrieve-default')

    def getConfiguration(self, requestor, nodeIdentifier):
        raise Unsupported('config-node')

    def setConfiguration(self, requestor, nodeIdentifier, options):
        raise Unsupported('config-node')

    def items(self, requestor, nodeIdentifier, maxItems, itemIdentifiers):
        raise Unsupported('retrieve-items')

    def retract(self, requestor, nodeIdentifier, itemIdentifiers):
        raise Unsupported('retract-items')

    def purge(self, requestor, nodeIdentifier):
        raise Unsupported('purge-nodes')

    def delete(self, requestor, nodeIdentifier):
        raise Unsupported('delete-nodes')
