import json
import logging

from cert_schema import jsonld_document_loader
from cert_schema import validate_unsigned_v1_2
from merkleproof.MerkleTree import MerkleTree
from merkleproof.MerkleTree import sha256
from pyld import jsonld
from werkzeug.contrib.cache import SimpleCache

from cert_issuer import tx_utils
from cert_issuer.errors import InsufficientFundsError
from cert_issuer.helpers import unhexlify, hexlify
from cert_issuer.issuer import Issuer
from cert_issuer.tx_utils import TransactionData

cache = SimpleCache()


def cached_document_loader(url, override_cache=False):
    if not override_cache:
        result = cache.get(url)
        if result:
            return result
    doc = jsonld_document_loader(url)
    cache.set(url, doc)
    return doc


class BatchIssuer(Issuer):
    def __init__(self, netcode, issuing_address, certificates_to_issue, connector, signer, batch_metadata,
                 tx_cost_constants):
        Issuer.__init__(self, netcode, issuing_address, certificates_to_issue, connector, signer)
        self.tree = MerkleTree(hash_f=sha256)
        self.batch_id = batch_metadata.batch_id
        self.batch_metadata = batch_metadata
        self.tx_cost_constants = tx_cost_constants

    def validate_schema(self):
        """
        Ensure certificates are valid v1.2 schema
        :return:
        """
        for _, certificate in self.certificates_to_issue.items():
            with open(certificate.signed_cert_file_name) as cert:
                cert_json = json.load(cert)
                validate_unsigned_v1_2(cert_json)

    def do_hash_certificate(self, certificate):
        """
        Hash the JSON-LD normalized certificate
        :param certificate:
        :return:
        """
        options = {'algorithm': 'URDNA2015', 'format': 'application/nquads', 'documentLoader': cached_document_loader}
        cert_utf8 = certificate.decode('utf-8')
        cert_json = json.loads(cert_utf8)
        normalized = jsonld.normalize(cert_json, options=options)
        hashed = sha256(normalized)
        self.tree.add_leaf(hashed, False)
        return hashed

    def calculate_cost_for_certificate_batch(self):
        """
        Per certificate, we pay 2*min_per_output (which is based on dust) + fee. Note assumes 1 input
        per tx.
        :return:
        """
        num_inputs = 1
        # output per recipient
        num_outputs = len(self.certificates_to_issue)
        # plus revocation outputs
        num_outputs += sum(1 for c in self.certificates_to_issue.values() if c.revocation_key)
        # plus global revocation, change output, and OP_RETURN
        num_outputs += 3
        self.total = tx_utils.calculate_tx_total(self.tx_cost_constants, num_inputs, num_outputs)
        return self.total

    def persist_tx(self, sent_tx_file_name, tx_id):
        Issuer.persist_tx(self, sent_tx_file_name, tx_id)
        # note that certificates are stored in an ordered dictionary, so we will iterate in the same order
        index = 0
        for uid, metadata in self.certificates_to_issue.items():
            receipt = self.tree.make_receipt(index, tx_id)

            with open(metadata.receipt_file_name, 'w') as out_file:
                out_file.write(json.dumps(receipt))

            with open(metadata.signed_cert_file_name, 'r') as in_file:
                signed_cert = json.load(in_file)

            blockchain_cert = {
                '@context': 'https://w3id.org/blockcerts/v1',
                'type': 'BlockchainCertificate',
                'document': signed_cert,
                'receipt': receipt
            }

            with open(metadata.blockchain_cert_file_name, 'w') as out_file:
                out_file.write(json.dumps(blockchain_cert))

            index += 1

    def create_transactions(self, revocation_address):
        """
        Create the batch Bitcoin transaction
        :param revocation_address:
        :return:
        """
        self.tree.make_tree()

        spendables = self.connector.get_unspent_outputs(self.issuing_address)
        if not spendables:
            error_message = 'No money to spend at address {}'.format(self.issuing_address)
            logging.error(error_message)
            raise InsufficientFundsError(error_message)

        last_input = spendables[-1]

        op_return_value = unhexlify(self.tree.get_merkle_root())

        tx_outs = self.build_recipient_tx_outs()
        tx_outs.append(tx_utils.create_transaction_output(revocation_address,
                                                          self.tx_cost_constants.get_minimum_output_coin()))

        transaction = tx_utils.create_trx(
            op_return_value,
            self.total,
            self.issuing_address,
            tx_outs,
            last_input)

        transaction_data = TransactionData(uid=self.batch_id,
                                           tx=transaction,
                                           tx_input=last_input,
                                           op_return_value=hexlify(op_return_value),
                                           batch_metadata=self.batch_metadata)

        return [transaction_data]

    def build_recipient_tx_outs(self):
        """
        Creates 2 transaction outputs for each recipient: one to their public key and the other to their specific
        revocation key.
        :return:
        """
        tx_outs = []
        for _, certificate in self.certificates_to_issue.items():
            recipient_outs = [tx_utils.create_transaction_output(certificate.public_key,
                                                                 self.tx_cost_constants.get_minimum_output_coin())]
            if certificate.revocation_key:
                recipient_outs.append(tx_utils.create_transaction_output(certificate.revocation_key,
                                                                         self.tx_cost_constants.get_minimum_output_coin()))

            tx_outs = tx_outs + recipient_outs

        return tx_outs
