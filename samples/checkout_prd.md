# Checkout — Payment Processing

## Overview

This document describes requirements for the checkout module's payment
handling, including retry behavior on transient failures.

## Requirements

When a payment attempt fails due to a transient gateway error, the system
automatically retries the charge up to 2 additional times before informing
the customer of a failure. Each retry waits at least 3 seconds longer than
the previous attempt.

If a card is declined for insufficient funds, the system does not retry and
immediately displays a message asking the customer to use a different
payment method.

The order total, including tax and shipping, must be locked at the moment
the customer confirms the order and must not change even if prices update
elsewhere in the catalog during checkout.

Every completed payment produces a receipt emailed to the customer within 5
minutes of the transaction being confirmed by the payment gateway.

Refunds can only be issued by staff with the "refund_approver" role, and
every refund is logged with the approver's identity, the original order ID,
and the refund amount.

The checkout page must display the accepted payment methods before the
customer enters any payment details.
