"""Enquiry Query Issue field mapping and resolution for Zendesk tickets."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Zendesk Custom Field IDs for Enquiry hierarchy
ENQUIRY_QUERY_ISSUE_FIELD_ID = 30888655400220
ENQUIRY_TEXT_FIELD_ID = 30970873867548
QUERY_TEXT_FIELD_ID = 30970858166812
QUERY_RELATED_ISSUE_TEXT_FIELD_ID = 30970917293852

DEFAULT_ENQUIRY_TAG = "ticket/_feedback__ticket_feedback_other__other"

RAW_ENQUIRY_QUERY_ISSUE_OPTIONS: List[Tuple[str, str]] = [
    ("Pre Sales Enquiry::Product Enquiry::Which product would suit my requirement?", "pre_sales_enquiry__product_enquiry__which_product_would_suit_my_requirement_"),
    ("Pre Sales Enquiry::Product Enquiry::Not able to locate the product I'm looking for", "pre_sales_enquiry__product_enquiry__not_able_to_locate_the_product_i_m_looking_for"),
    ("Pre Sales Enquiry::Product Enquiry::I have a material/ hardware related enquriy", "pre_sales_enquiry__product_enquiry__i_have_a_material/_hardware_related_enquriy"),
    ("Pre Sales Enquiry::Product Enquiry::Installation", "pre_sales_enquiry__product_enquiry__installation"),
    ("Pre Sales Enquiry::Product Enquiry::How to measure the dimensions of the product", "pre_sales_enquiry__product_enquiry__how_to_measure_the_dimensions_of_the_product"),
    ("Pre Sales Enquiry::Product Enquiry::New Product, Material or Finishing option", "pre_sales_enquiry__product_enquiry__new_product__material_or_finishing_option"),
    ("Pre Sales Enquiry::Product Enquiry::Product Add-ons related query", "pre_sales_enquiry__product_enquiry__product_add-ons_related_query"),
    ("Pre Sales Enquiry::Product Enquiry::Package dimensions information required", "pre_sales_enquiry__product_enquiry__package_dimensions_information_required"),
    ("Pre Sales Enquiry::Product Enquiry::Production unit location", "pre_sales_enquiry__product_enquiry__production_unit_location"),
    ("Pre Sales Enquiry::Product Enquiry::Product maintenance related query", "pre_sales_enquiry__product_enquiry__product_maintenance_related_query"),
    ("Pre Sales Enquiry::Product Enquiry::Sample kit", "pre_sales_enquiry__product_enquiry__sample_kit"),
    ("Pre Sales Enquiry::Product Enquiry::Charity Banner", "pre_sales_enquiry__product_enquiry__charity_banner"),
    ("Pre Sales Enquiry::Product Enquiry::OTHER", "pre_sales_enquiry__product_enquiry__other"),
    ("Pre Sales Enquiry::Pricing Enquiry::Quote", "pre_sales_enquiry__pricing_enquiry__quote"),
    ("Pre Sales Enquiry::Pricing Enquiry::I want to know if I'm eligible for product / promo discounts", "pre_sales_enquiry__pricing_enquiry__i_want_to_know_if_i_m_eligible_for_product_/_promo_discounts"),
    ("Pre Sales Enquiry::Pricing Enquiry::Dealer Pricing / Discount", "pre_sales_enquiry__pricing_enquiry__dealer_pricing_/_discount"),
    ("Pre Sales Enquiry::Pricing Enquiry::Shipping rates discount", "pre_sales_enquiry__pricing_enquiry__shipping_rates_discount"),
    ("Pre Sales Enquiry::Pricing Enquiry::My coupon code is not working", "pre_sales_enquiry__pricing_enquiry__my_coupon_code_is_not_working"),
    ("Pre Sales Enquiry::Pricing Enquiry::Vectorization", "pre_sales_enquiry__pricing_enquiry__vectorization"),
    ("Pre Sales Enquiry::Pricing Enquiry::Wallet Points", "pre_sales_enquiry__pricing_enquiry__wallet_points"),
    ("Pre Sales Enquiry::Pricing Enquiry::Price Match", "pre_sales_enquiry__pricing_enquiry__price_match"),
    ("Pre Sales Enquiry::Pricing Enquiry::OTHER", "pre_sales_enquiry__pricing_enquiry__other"),
    ("Pre Sales Enquiry::Design::I was not able to upload my artwork", "pre_sales_enquiry__design__i_was_not_able_to_upload_my_artwork"),
    ("Pre Sales Enquiry::Design::Problem with Design Tool", "pre_sales_enquiry__design__problem_with_design_tool"),
    ("Pre Sales Enquiry::Design::How to Design with Design Tool", "pre_sales_enquiry__design__how_to_design_with_design_tool"),
    ("Pre Sales Enquiry::Design::File Format or Templates", "pre_sales_enquiry__design__file_format_or_templates"),
    ("Pre Sales Enquiry::Design::Check file and send proofs", "pre_sales_enquiry__design__check_file_and_send_proofs"),
    ("Pre Sales Enquiry::Design::OTHER", "pre_sales_enquiry__design__other"),
    ("Pre Sales Enquiry::I have issues in placing my order::The payment link is not working", "pre_sales_enquiry__i_have_issues_in_placing_my_order__the_payment_link_is_not_working"),
    ("Pre Sales Enquiry::I have issues in placing my order::Not able to place order", "pre_sales_enquiry__i_have_issues_in_placing_my_order__not_able_to_place_order"),
    ("Pre Sales Enquiry::I have issues in placing my order::The website is getting stuck / Freeze", "pre_sales_enquiry__i_have_issues_in_placing_my_order__the_website_is_getting_stuck_/_freeze"),
    ("Pre Sales Enquiry::I have issues in placing my order::My pincode is not serviceable", "pre_sales_enquiry__i_have_issues_in_placing_my_order__my_pincode_is_not_serviceable"),
    ("Pre Sales Enquiry::I have issues in placing my order::Unable to use Coupon Code", "pre_sales_enquiry__i_have_issues_in_placing_my_order__unable_to_use_coupon_code"),
    ("Pre Sales Enquiry::I have issues in placing my order::Unable to login", "pre_sales_enquiry__i_have_issues_in_placing_my_order__unable_to_login"),
    ("Pre Sales Enquiry::I have issues in placing my order::Website Concern", "pre_sales_enquiry__i_have_issues_in_placing_my_order__website_concern"),
    ("Pre Sales Enquiry::I have issues in placing my order::OTHER", "pre_sales_enquiry__i_have_issues_in_placing_my_order__other"),
    ("Pre Sales Enquiry::Delivery Date (ETA)::Shipping method related query", "pre_sales_enquiry__delivery_date__eta___shipping_method_related_query"),
    ("Pre Sales Enquiry::Delivery Date (ETA)::Non deliverable locations", "pre_sales_enquiry__delivery_date__eta___non_deliverable_locations"),
    ("Pre Sales Enquiry::Delivery Date (ETA)::Require faster shipping", "pre_sales_enquiry__delivery_date__eta___require_faster_shipping"),
    ("Pre Sales Enquiry::Delivery Date (ETA)::Multiple shipments", "pre_sales_enquiry__delivery_date__eta___multiple_shipments"),
    ("Pre Sales Enquiry::Dealer account::New enquiry", "pre_sales_enquiry__dealer_account__new_enquiry"),
    ("Pre Sales Enquiry::Dealer account::Sample kit enquiry", "pre_sales_enquiry__dealer_account__sample_kit_enquiry"),
    ("Pre Sales Enquiry::Dealer account::Registration related query", "pre_sales_enquiry__dealer_account__registration_related_query"),
    ("Pre Sales Enquiry::Dealer account::Sales Approach/ Affiliate marketing", "pre_sales_enquiry__dealer_account__sales_approach/_affiliate_marketing"),
    ("Pre Sales Enquiry::Reorder/ Repeat order::Reorder Within 180 days", "pre_sales_enquiry__reorder/_repeat_order__reorder_within_180_days"),
    ("Pre Sales Enquiry::Reorder/ Repeat order::Reorder More than 180 days", "pre_sales_enquiry__reorder/_repeat_order__reorder_more_than_180_days"),
    ("Pre Sales Enquiry::Reorder/ Repeat order::Reorder with product changes", "pre_sales_enquiry__reorder/_repeat_order__reorder_with_product_changes"),
    ("Pre Sales Enquiry::No response::Customer left after the chat after one response", "pre_sales_enquiry__no_response__customer_left_after_the_chat_after_one_response"),
    ("Pre Sales Enquiry::No response::Inaudible/ no response from the customer", "pre_sales_enquiry__no_response__inaudible/_no_response_from_the_customer"),
    ("Post Sales Enquiry::Order Enquiry::When will I have the Proof", "post_sales_enquiry__order_enquiry__when_will_i_have_the_proof"),
    ("Post Sales Enquiry::Order Enquiry::When will my shipment be delivered?", "post_sales_enquiry__order_enquiry__when_will_my_shipment_be_delivered_"),
    ("Post Sales Enquiry::Order Enquiry::Opt for Proof", "post_sales_enquiry__order_enquiry__opt_for_proof"),
    ("Post Sales Enquiry::Order Enquiry::Approve Proof", "post_sales_enquiry__order_enquiry__approve_proof"),
    ("Post Sales Enquiry::Order Enquiry::Order Confirmation", "post_sales_enquiry__order_enquiry__order_confirmation"),
    ("Post Sales Enquiry::Order Enquiry::Discount", "post_sales_enquiry__order_enquiry__discount"),
    ("Post Sales Enquiry::Order Enquiry::Unable to login", "post_sales_enquiry__order_enquiry__unable_to_login"),
    ("Post Sales Enquiry::Order Enquiry::Wallet Points", "post_sales_enquiry__order_enquiry__wallet_points"),
    ("Post Sales Enquiry::Order Enquiry::Website Concern", "post_sales_enquiry__order_enquiry__website_concern"),
    ("Post Sales Enquiry::Order Enquiry::Authorize delivery without signature", "post_sales_enquiry__order_enquiry__authorize_delivery_without_signature"),
    ("Post Sales Enquiry::Order Enquiry::Order Status", "post_sales_enquiry__order_enquiry__order_status"),
    ("Post Sales Enquiry::Order Enquiry::Product maintenance related query", "post_sales_enquiry__order_enquiry__product_maintenance_related_query"),
    ("Post Sales Enquiry::Order Enquiry::OTHER", "post_sales_enquiry__order_enquiry__other"),
    ("Post Sales Enquiry::Design::Has my file been uploaded", "post_sales_enquiry__design__has_my_file_been_uploaded"),
    ("Post Sales Enquiry::Design::New Artwork / Customer Comment", "post_sales_enquiry__design__new_artwork_/_customer_comment"),
    ("Post Sales Enquiry::Design::Vectorization", "post_sales_enquiry__design__vectorization"),
    ("Post Sales Enquiry::Design::I was not able to upload my artwork", "post_sales_enquiry__design__i_was_not_able_to_upload_my_artwork"),
    ("Post Sales Enquiry::Design::File Format or Templates", "post_sales_enquiry__design__file_format_or_templates"),
    ("Post Sales Enquiry::Design::Proof Questions", "post_sales_enquiry__design__proof_questions"),
    ("Post Sales Enquiry::Design::Talk with Designer", "post_sales_enquiry__design__talk_with_designer"),
    ("Post Sales Enquiry::Design::Unable to view Artwork/ Designer Comment", "post_sales_enquiry__design__unable_to_view_artwork/_designer_comment"),
    ("Post Sales Enquiry::Design::OTHER", "post_sales_enquiry__design__other"),
    ("Post Sales Enquiry::Modify my order::I want to change quantity / size / shipping", "post_sales_enquiry__modify_my_order__i_want_to_change_quantity_/_size_/_shipping"),
    ("Post Sales Enquiry::Modify my order::I want to add/change/remove product", "post_sales_enquiry__modify_my_order__i_want_to_add/change/remove_product"),
    ("Post Sales Enquiry::Modify my order::Change billing / shipping address", "post_sales_enquiry__modify_my_order__change_billing_/_shipping_address"),
    ("Post Sales Enquiry::Modify my order::I want to schedule for early or later delivery / Attach Label when shipping", "post_sales_enquiry__modify_my_order__i_want_to_schedule_for_early_or_later_delivery_/_attach_label_when_shipping"),
    ("Post Sales Enquiry::Modify my order::Select Drop Ship", "post_sales_enquiry__modify_my_order__select_drop_ship"),
    ("Post Sales Enquiry::Modify my order::Ship to multiple address", "post_sales_enquiry__modify_my_order__ship_to_multiple_address"),
    ("Post Sales Enquiry::Modify my order::Combine Shipment", "post_sales_enquiry__modify_my_order__combine_shipment"),
    ("Post Sales Enquiry::Modify my order::OTHER", "post_sales_enquiry__modify_my_order__other"),
    ("Post Sales Enquiry::No response::Customer left after the chat after one response", "post_sales_enquiry__no_response__customer_left_after_the_chat_after_one_response"),
    ("Post Sales Enquiry::No response::Inaudible/ no response from the customer", "post_sales_enquiry__no_response__inaudible/_no_response_from_the_customer"),
    ("Post Sales Enquiry::Payment related queries::Make payment", "post_sales_enquiry__payment_related_queries__make_payment"),
    ("Post Sales Enquiry::Payment related queries::Unable to make the payment", "post_sales_enquiry__payment_related_queries__unable_to_make_the_payment"),
    ("Post Sales Enquiry::Payment related queries::Understanding due amount", "post_sales_enquiry__payment_related_queries__understanding_due_amount"),
    ("Post Sales Enquiry::Payment related queries::Refund Status", "post_sales_enquiry__payment_related_queries__refund_status"),
    ("Post Sales Enquiry::Payment related queries::Dissatisfied with the order changes/ due amount", "post_sales_enquiry__payment_related_queries__dissatisfied_with_the_order_changes/_due_amount"),
    ("Post Sales Enquiry::After order goes to production::Artwork change requested", "post_sales_enquiry__after_order_goes_to_production__artwork_change_requested"),
    ("Post Sales Enquiry::After order goes to production::Product change requested", "post_sales_enquiry__after_order_goes_to_production__product_change_requested"),
    ("Post Sales Enquiry::After order goes to production::Quantity change requested", "post_sales_enquiry__after_order_goes_to_production__quantity_change_requested"),
    ("Post Sales Enquiry::After order goes to production::Shipping method change requested", "post_sales_enquiry__after_order_goes_to_production__shipping_method_change_requested"),
    ("Post Sales Enquiry::After order goes to production::Address change requested", "post_sales_enquiry__after_order_goes_to_production__address_change_requested"),
    ("Cancellation::Customer::No longer required/ Event cancelled", "cancellation__customer__no_longer_required/_event_cancelled"),
    ("Cancellation::Customer::Order placed with incorrect details", "cancellation__customer__order_placed_with_incorrect_details"),
    ("Cancellation::Customer::Delay from customer", "cancellation__customer__delay_from_customer"),
    ("Cancellation::Customer::I found a cheaper price elsewhere", "cancellation__customer__i_found_a_cheaper_price_elsewhere"),
    ("Cancellation::Customer::The shipment will not arrive by the time I need it", "cancellation__customer__the_shipment_will_not_arrive_by_the_time_i_need_it"),
    ("Cancellation::Customer::Duplicate Order", "cancellation__customer__duplicate_order"),
    ("Cancellation::Customer::No reason provided", "cancellation__customer__no_reason_provided"),
    ("Cancellation::Artwork Issue::Orientation issue", "cancellation__artwork_issue__orientation_issue"),
    ("Cancellation::Artwork Issue::Low Resolution file", "cancellation__artwork_issue__low_resolution_file"),
    ("Cancellation::Artwork Issue::File missing/ not received", "cancellation__artwork_issue__file_missing/_not_received"),
    ("Cancellation::Design::Back and forth from design team", "cancellation__design__back_and_forth_from_design_team"),
    ("Cancellation::Design::Delay from Design team", "cancellation__design__delay_from_design_team"),
    ("Cancellation::Design::Comment missed from Design team", "cancellation__design__comment_missed_from_design_team"),
    ("Cancellation::CS team::Back and forth from CS team", "cancellation__cs_team__back_and_forth_from_cs_team"),
    ("Cancellation::CS team::Poor customer service", "cancellation__cs_team__poor_customer_service"),
    ("Cancellation::CS team::Comment missed from CS team", "cancellation__cs_team__comment_missed_from_cs_team"),
    ("Cancellation::Website/ Admin::Wallet Points related query", "cancellation__website/_admin__wallet_points_related_query"),
    ("Cancellation::Website/ Admin::Website issues", "cancellation__website/_admin__website_issues"),
    ("Cancellation::Website/ Admin::Order confirmation not received", "cancellation__website/_admin__order_confirmation_not_received"),
    ("Cancellation::Website/ Admin::Unable to apply discount", "cancellation__website/_admin__unable_to_apply_discount"),
    ("Cancellation::Website/ Admin::Store credit related query", "cancellation__website/_admin__store_credit_related_query"),
    ("Ticket/ Feedback::The product is not appropriate::Wrinkles / Deep imprint on the product", "ticket/_feedback__the_product_is_not_appropriate__wrinkles_/_deep_imprint_on_the_product"),
    ("Ticket/ Feedback::The product is not appropriate::Color discrepancy / Printing Issue", "ticket/_feedback__the_product_is_not_appropriate__color_discrepancy_/_printing_issue"),
    ("Ticket/ Feedback::The product is not appropriate::Finishing issue", "ticket/_feedback__the_product_is_not_appropriate__finishing_issue"),
    ("Ticket/ Feedback::The product is not appropriate::Print File Error (Spelling error, Footprint, etc)", "ticket/_feedback__the_product_is_not_appropriate__print_file_error__spelling_error__footprint__etc_"),
    ("Ticket/ Feedback::The product is not appropriate::OTHER", "ticket/_feedback__the_product_is_not_appropriate__other"),
    ("Ticket/ Feedback::Order wrong stuff::Product ordered other then required", "ticket/_feedback__order_wrong_stuff__product_ordered_other_then_required"),
    ("Ticket/ Feedback::Order wrong stuff::Missed to add Hardware / Print", "ticket/_feedback__order_wrong_stuff__missed_to_add_hardware_/_print"),
    ("Ticket/ Feedback::The product is a malfunction::Water leakage in cover", "ticket/_feedback__the_product_is_a_malfunction__water_leakage_in_cover"),
    ("Ticket/ Feedback::The product is a malfunction::Product issue (Cracks, print fading,etc)", "ticket/_feedback__the_product_is_a_malfunction__product_issue__cracks__print_fading_etc_"),
    ("Ticket/ Feedback::The product is a malfunction::Hardware issue", "ticket/_feedback__the_product_is_a_malfunction__hardware_issue"),
    ("Ticket/ Feedback::The product is a malfunction::Accessories/tie-down not functional", "ticket/_feedback__the_product_is_a_malfunction__accessories/tie-down_not_functional"),
    ("Ticket/ Feedback::The product is a malfunction::OTHER", "ticket/_feedback__the_product_is_a_malfunction__other"),
    ("Ticket/ Feedback::I have received a different product / someone else order::Received different product", "ticket/_feedback__i_have_received_a_different_product_/_someone_else_order__received_different_product"),
    ("Ticket/ Feedback::I have received a different product / someone else order::Received someone else order", "ticket/_feedback__i_have_received_a_different_product_/_someone_else_order__received_someone_else_order"),
    ("Ticket/ Feedback::Missing Product / Parts / Accessories::Missed to ship", "ticket/_feedback__missing_product_/_parts_/_accessories__missed_to_ship"),
    ("Ticket/ Feedback::Missing Product / Parts / Accessories::Product shipped but not received", "ticket/_feedback__missing_product_/_parts_/_accessories__product_shipped_but_not_received"),
    ("Ticket/ Feedback::Installation issue/ product damaged::Product received damaged", "ticket/_feedback__installation_issue/_product_damaged__product_received_damaged"),
    ("Ticket/ Feedback::Installation issue/ product damaged::Damaged under warranty", "ticket/_feedback__installation_issue/_product_damaged__damaged_under_warranty"),
    ("Ticket/ Feedback::Installation issue/ product damaged::Damaged during installation", "ticket/_feedback__installation_issue/_product_damaged__damaged_during_installation"),
    ("Ticket/ Feedback::Installation issue/ product damaged::Fitting issue", "ticket/_feedback__installation_issue/_product_damaged__fitting_issue"),
    ("Ticket/ Feedback::Installation issue/ product damaged::OTHER", "ticket/_feedback__installation_issue/_product_damaged__other"),
    ("Ticket/ Feedback::Never received the product::Auto store credit", "ticket/_feedback__never_received_the_product__auto_store_credit"),
    ("Ticket/ Feedback::Never received the product::Delay in delivery", "ticket/_feedback__never_received_the_product__delay_in_delivery"),
    ("Ticket/ Feedback::Never received the product::Package lost in production", "ticket/_feedback__never_received_the_product__package_lost_in_production"),
    ("Ticket/ Feedback::Never received the product::Package lost in transit", "ticket/_feedback__never_received_the_product__package_lost_in_transit"),
    ("Ticket/ Feedback::Never received the product::Non receipt of package", "ticket/_feedback__never_received_the_product__non_receipt_of_package"),
    ("Ticket/ Feedback::Never received the product::OTHER", "ticket/_feedback__never_received_the_product__other"),
    ("Ticket/ Feedback::Store credit::Asking for Refund (Auto Store Credit)", "ticket/_feedback__store_credit__asking_for_refund__auto_store_credit_"),
    ("Ticket/ Feedback::Store credit::Replace product, size, qty. / Cancel / Shipping", "ticket/_feedback__store_credit__replace_product__size__qty._/_cancel_/_shipping"),
    ("Ticket/ Feedback::Delight customer::Customer adamant", "ticket/_feedback__delight_customer__customer_adamant"),
    ("Ticket/ Feedback::Delight customer::Potential customer", "ticket/_feedback__delight_customer__potential_customer"),
    ("Ticket/ Feedback::Delight customer::Human error / First time customer", "ticket/_feedback__delight_customer__human_error_/_first_time_customer"),
    ("Ticket/ Feedback::Delight customer::Policy Process", "ticket/_feedback__delight_customer__policy_process"),
    ("Ticket/ Feedback::Delight customer::Process Improvement", "ticket/_feedback__delight_customer__process_improvement"),
    ("Ticket/ Feedback::Feedback::Negative", "ticket/_feedback__feedback__negative"),
    ("Ticket/ Feedback::Feedback::Positive", "ticket/_feedback__feedback__positive"),
    ("Ticket/ Feedback::Cancellation::Artwork related issue", "ticket/_feedback__cancellation__artwork_related_issue"),
    ("Ticket/ Feedback::Cancellation::I found a cheaper price elsewhere", "ticket/_feedback__cancellation__i_found_a_cheaper_price_elsewhere"),
    ("Ticket/ Feedback::Cancellation::The shipment will not arrive by the time I need it", "ticket/_feedback__cancellation__the_shipment_will_not_arrive_by_the_time_i_need_it"),
    ("Ticket/ Feedback::Cancellation::No longer required / Event Cancelled", "ticket/_feedback__cancellation__no_longer_required_/_event_cancelled"),
    ("Ticket/ Feedback::Cancellation::Duplicate Order", "ticket/_feedback__cancellation__duplicate_order"),
    ("Ticket/ Feedback::Cancellation::OTHER", "ticket/_feedback__cancellation__other"),
    ("Ticket/ Feedback::Ticket_Feedback_OTHER::Admin / Website Error", "ticket/_feedback__ticket_feedback_other__admin_/_website_error"),
    ("Ticket/ Feedback::Ticket_Feedback_OTHER::OTHER", "ticket/_feedback__ticket_feedback_other__other"),
    ("Follow up Email::Artwork approval", "follow_up_email__artwork_approval"),
    ("Follow up Email::Payment requested", "follow_up_email__payment_requested"),
    ("Follow up Email::Low resolution artwork", "follow_up_email__low_resolution_artwork"),
    ("Follow up Email::File missing", "follow_up_email__file_missing"),
    ("Follow up Email::Designer Comment", "follow_up_email__designer_comment"),
    ("Follow up Email::One Day Delay", "follow_up_email__one_day_delay"),
    ("Follow up Email::Order Delay", "follow_up_email__order_delay"),
    ("Follow up Email::Tracking Information", "follow_up_email__tracking_information"),
    ("Follow up Email::Out Of Stock", "follow_up_email__out_of_stock"),
    ("Follow up Email::Design Followup Call", "follow_up_email__design_followup_call"),
    ("Follow up Email::Automated Call Follow-up", "follow_up_email__automated_call_follow-up"),
    ("Vendor::Order placed with vendor", "vendor__order_placed_with_vendor"),
    ("Vendor::Vendor Artwork Approval", "vendor__vendor_artwork_approval"),
    ("Vendor::Order update", "vendor__order_update"),
    ("Vendor::Proof Requested with Vendor", "vendor__proof_requested_with_vendor"),
    ("Vendor::Update tracking code", "vendor__update_tracking_code"),
    ("Other Emails::Company's promo / Internal Email", "other_emails__company_s_promo_/_internal_email"),
    ("Other Emails::Invoice::Tax Exempt", "other_emails__invoice__tax_exempt"),
    ("Other Emails::Invoice::Multiple Charge", "other_emails__invoice__multiple_charge"),
    ("Other Emails::Invoice::Invoice Copy", "other_emails__invoice__invoice_copy"),
    ("Other Emails::Location", "other_emails__location"),
    ("Other Emails::Customer's Auto Respnse (Holiday)", "other_emails__customer_s_auto_respnse__holiday_"),
    ("Other Emails::Paypal Notification Email", "other_emails__paypal_notification_email"),
    ("Other Emails::Order Notification Email", "other_emails__order_notification_email"),
    ("Other Emails::Logistics/ Logistics Notification Email", "other_emails__logistics/_logistics_notification_email"),
    ("Other Emails::Call::Missed", "other_emails__call__missed"),
    ("Other Emails::Call::Notification", "other_emails__call__notification"),
    ("Other Emails::Chat::Missed", "other_emails__chat__missed"),
    ("Other Emails::Chat::Notification", "other_emails__chat__notification"),
    ("Other Emails::Chat::Chat Bot", "other_emails__chat__chat_bot"),
    ("Other Emails::Incorrect Email/ Email bounceback", "other_emails__incorrect_email/_email_bounceback"),
    ("Other Emails::Report Email", "other_emails__report_email"),
    ("Other Emails::Marketing Email", "other_emails__marketing_email"),
    ("Other Emails::Irrelevant Email", "other_emails__irrelevant_email"),
    ("Other Emails::Fraudlent Charges / CC used somewhere else", "other_emails__fraudlent_charges_/_cc_used_somewhere_else"),
]

_EQI_FULL_MAP: Dict[str, str] = {}
_EQI_TWO_MAP: Dict[str, str] = {}
_EQI_LEAF_MAP: Dict[str, str] = {}

def _eqi_norm(val: Any) -> str:
    if not val:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(val).lower())

for _name, _val in RAW_ENQUIRY_QUERY_ISSUE_OPTIONS:
    _parts = [p.strip() for p in _name.split("::")]
    if len(_parts) == 3:
        _k3 = f"{_eqi_norm(_parts[0])}::{_eqi_norm(_parts[1])}::{_eqi_norm(_parts[2])}"
        _EQI_FULL_MAP[_k3] = _val
        _k2 = f"{_eqi_norm(_parts[0])}::{_eqi_norm(_parts[1])}"
        if _k2 not in _EQI_TWO_MAP:
            _EQI_TWO_MAP[_k2] = _val
        _leaf_k = _eqi_norm(_parts[2])
        if _leaf_k not in _EQI_LEAF_MAP:
            _EQI_LEAF_MAP[_leaf_k] = _val
    elif len(_parts) == 2:
        _k2 = f"{_eqi_norm(_parts[0])}::{_eqi_norm(_parts[1])}"
        _EQI_TWO_MAP[_k2] = _val
        _leaf_k = _eqi_norm(_parts[1])
        if _leaf_k not in _EQI_LEAF_MAP:
            _EQI_LEAF_MAP[_leaf_k] = _val

_EQI_ENQUIRY_FALLBACKS: Dict[str, str] = {
    _eqi_norm("Pre Sales Enquiry"): "pre_sales_enquiry__product_enquiry__other",
    _eqi_norm("Post Sales Enquiry"): "post_sales_enquiry__order_enquiry__other",
    _eqi_norm("Ticket / Feedback"): "ticket/_feedback__ticket_feedback_other__other",
    _eqi_norm("Cancellation"): "ticket/_feedback__cancellation__other",
    _eqi_norm("Vendor"): "vendor__order_update",
    _eqi_norm("Follow up Email"): "follow_up_email__designer_comment",
    _eqi_norm("Followup Email"): "follow_up_email__designer_comment",
    _eqi_norm("Other Emails"): "other_emails__irrelevant_email",
}

def resolve_enquiry_query_issue(
    enquiry: Optional[str] = None,
    query: Optional[str] = None,
    issue: Optional[str] = None,
) -> str:
    """Resolve Salesforce Enquiry/Query/Issue to Zendesk Enquiry Query Issue dropdown tag."""
    ne = _eqi_norm(enquiry)
    nq = _eqi_norm(query)
    ni = _eqi_norm(issue)

    if ne and nq and ni:
        k3 = f"{ne}::{nq}::{ni}"
        if k3 in _EQI_FULL_MAP:
            return _EQI_FULL_MAP[k3]

    if ne and nq:
        k2 = f"{ne}::{nq}"
        if k2 in _EQI_TWO_MAP:
            return _EQI_TWO_MAP[k2]

    if ni and ni in _EQI_LEAF_MAP:
        return _EQI_LEAF_MAP[ni]

    if ne in _EQI_ENQUIRY_FALLBACKS:
        return _EQI_ENQUIRY_FALLBACKS[ne]

    return DEFAULT_ENQUIRY_TAG
