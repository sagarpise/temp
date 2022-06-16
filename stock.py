import json
import logging
import re
from datetime import datetime

import requests

from openerp import SUPERUSER_ID
from openerp import netsvc
from openerp.osv import osv, fields
from openerp.tools.translate import _

_logger = logging.getLogger(__name__)


class wms_skipped_response(osv.osv):
    _name = 'wms.skipped.response'

    _columns = {
        'method': fields.selection([('ASNCLOSED', 'ASNCLOSED'), ('ORDERSHIPPED', 'ORDERSHIPPED'), ('TRANSFERFINALIZED', 'TRANSFERFINALIZED'),
                                    ('PARTIALSHIPMENT', 'PARTIALSHIPMENT'), ('ORDERSHIPPED', 'ORDERSHIPPED')], 'Method', readonly=True),
        'response': fields.text('Response', readonly=True),
        'last_message': fields.text('Last Message', readonly=True),
        'create_date': fields.datetime("Created on", select=True, readonly=True),
        'write_date': fields.datetime("Modification Date", select=True, readonly=True),
    }

    def button_retry(self, cr, uid, ids, context=None):
        for rec in self.browse(cr, uid, ids):
            if rec.method == 'ASNCLOSED':
                error_resp = self.pool.get('stock.picking').process_asn_closed_wms(cr, uid, json.loads(rec.response), context=context)
                if error_resp:
                    self.write(cr, uid, [rec.id], {'last_message': error_resp})
                else:
                    self.unlink(cr, uid, [rec.id])

            elif rec.method == 'ORDERSHIPPED':
                error_resp = self.pool.get('stock.picking').process_order_shipped_wms(cr, uid, json.loads(rec.response), context=context)
                if error_resp:
                    self.write(cr, uid, [rec.id], {'last_message': error_resp})
                else:
                    self.unlink(cr, uid, [rec.id])

            elif rec.method == 'TRANSFERFINALIZED':
                warehouse_name = 'ATLASUSA_TST_ATLASUSA_TST_SCE_PRD_0_wmwhse1'
                wms_details = self.pool.get('wms.config.settings').default_get(cr, SUPERUSER_ID, [], context=context)
                error_resp = self.pool.get('stock.picking').process_transfer_finalized_wms(cr, uid, json.loads(rec.response), wms_details,
                                                                                           warehouse_name, context=context)
                if error_resp:
                    self.write(cr, uid, [rec.id], {'last_message': error_resp})
                else:
                    self.unlink(cr, uid, [rec.id])

            elif rec.method == 'PARTIALSHIPMENT':
                pass

            elif rec.method == 'ORDERSHIPPED':
                pass

        return True


class stock_picking(osv.osv):
    _inherit = 'stock.picking'
    _columns = {
        'wms_api_order_id': fields.char('WMS API Order Id', readonly=True)
    }

    def copy(self, cr, uid, id, default=None, context=None):
        if default is None:
            default = {}
        default = default.copy()
        default.update({'wms_api_order_id': None})
        return super(stock_picking, self).copy(cr, uid, id, default, context)

    # def action_confirm(self, cr, uid, ids, context=None):
    #     """had to call export here because this method calls write before whole functionality"""
    #     res = super(stock_picking, self).action_confirm(cr, uid, ids, context=context)
    #     csv_obj = self.pool.get('csv.import.export')
    #     model, location = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'atlas_wms_integration', 'stock_location_colton')
    #     flag = 0
    #     for picking in self.browse(cr, uid, ids):
    #         picking.refresh()
    #         for move in picking.move_lines:
    #             move.refresh()
    #             if move.location_id.id == location and picking.type == 'out' and picking.state == 'confirmed':
    #                 flag = 1
    #             elif move.location_dest_id.id == location and picking.type == 'in' and picking.state == 'confirmed':
    #                 flag = 2
    #         # # Temporary commented because after some time Joey want to set default location to colton instead of Stock for DO
    #         # if flag == 1:
    #         #     self.wms_api_out_export(cr, uid, [picking.id], context=context)
    #         if flag == 2:
    #             self.wms_api_in_export(cr, uid, picking, context=context)
    #     return res

    def split_move_prodlot_id(self, cr, uid, move_id, serial_qty_dict, context=None):
        if not context:
            context = {}
        context.update({'active_model': 'stock.move'})
        line_exist_ids = []
        move = self.pool.get('stock.move').browse(cr, uid, move_id, context=context)
        split_wizard = self.pool.get('stock.move.split')
        for prodlot_id, quantity in serial_qty_dict.items():
            line_exist_ids.append((0, 0, {'prodlot_id': prodlot_id, 'quantity': quantity}))
        wizard = split_wizard.create(cr, uid, {
            'product_id': move.product_id.id,
            'product_uom': move.product_uom.id,
            'qty': move.product_qty,
            'location_id': move.location_id.id,
            'use_exist': True,
            'line_exist_ids': line_exist_ids,
        })
        return split_wizard.split(cr, uid, [wizard], [move.id], context=context), ''

    def refresh_wms_token(self, cr, uid):
        wms_details = self.pool.get('wms.config.settings').default_get(cr, SUPERUSER_ID, [])
        wms_access_token_url = wms_details.get('wms_access_token_url')
        wms_refresh_token = wms_details.get('wms_refresh_token')
        wms_client_id = wms_details.get('wms_client_id')
        wms_client_secrete = wms_details.get('wms_client_secrete')

        payload = {'grant_type': 'refresh_token', 'refresh_token': wms_refresh_token}
        access_token_response = requests.post(wms_access_token_url, data=payload, auth=(wms_client_id, wms_client_secrete),
                                              verify=False, allow_redirects=False)
        response = json.loads(access_token_response.text)
        print "\n\n\nresponse:\n", response
        if access_token_response.status_code == 200 and response.get('access_token'):
            self.pool.get('ir.config_parameter').set_param(cr, uid, 'wms.access_token', response.get('access_token') or '')
            # self.pool.get('ir.config_parameter').set_param(cr, uid, 'wms.refresh_token', response.get('refresh_token') or '')
            return True, response.get('access_token')
        elif access_token_response.status_code > 200:
            return False, access_token_response.content
        return False, ''

    def wms_api_in_export(self, cr, uid, picking, context=None):
        wms_details = self.pool.get('wms.config.settings').default_get(cr, SUPERUSER_ID, [], context=context)
        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')
        storerkey = wms_details.get('wms_store_key')
        owner = wms_details.get('wms_owner')
        wms_warehouse_name = 'ATLASUSA_PRD_ATLASUSA_PRD_SCE_PRD_0_wmwhse1'  # 'ATLASUSA_TST_ATLASUSA_TST_SCE_PRD_0_wmwhse1'
        carrierreference = picking.container_number and picking.container_number[:17] or ''
        externalreceiptkey2 = picking.origin or ''
        externreceiptkey = picking.name or ''
        receiptkey = picking.name.split('/')[1]
        asn_dict = {
            'receiptkey': receiptkey,
            'addwho': 'wmwhse1',
            'carrierreference': carrierreference,
            'carrierroutestatus': 'NEW',
            'editwho': 'wmwhse1',
            'externalreceiptkey2': externalreceiptkey2,
            'forteflag': 'I',
            'lottablematchrequired': '1',
            'storerkey': storerkey,
            'type': 1,  # 1:Normal 2:CustomerReturn 7:PurchaseOrder 8:Production Order 6:AdvanceShipNotice 9:Transfer 10:ProductionOrderReturn
            'receiptdetails': [],
            'itrns': [],
            'whseid': 'wmwhse1',
            'externreceiptkey': externreceiptkey,
        }
        asn_list = []
        receiptlinenumber = 0
        for move in picking.move_lines:
            prod = move.product_id
            sku = prod.default_code or prod.magento_sku
            search_item_endpoint = wms_api_endpoint + '/' + wms_warehouse_name + '/items/' + sku + '/' + owner
            params = {'access_token': wms_access_token}
            search_item_resp = requests.request("GET", search_item_endpoint, data={}, params=params, headers={'Content-Type': 'application/json'})
            if search_item_resp.status_code == 200:
                pass

            elif search_item_resp.status_code == 401:  # unauthorised needs to refresh token
                raise osv.except_osv(_("Refresh Token:"), _(search_item_resp.content))
            else:
                raise osv.except_osv(_("Data Error"), _("Product with SKU %s is not present in the WMS system" % sku))
                # # Create A New Product
                # stdnetwgt = prod.magento_exportable and prod.x_magerp_ship_weight or prod.weight_net or 0
                # stdgrosswgt = prod.magento_exportable and prod.x_magerp_prod_weight or prod.weight or 0
                # wh_id = 'ATLASUSA_TST_ENTERPRISE'  # Here we can use only ATLASUSA_TST_ENTERPRISE as a warehouse because it is parent wahrehouse
                # url = wms_api_endpoint + '/' + wh_id + '/items'
                # payload = json.dumps({
                #     "storerkey": storerkey,
                #     "sku": sku and sku[:50] or '',
                #     "descr": prod.description and prod.description[:50] or '',  # 254
                #     "packkey": "STD",
                #     "stdgrosswgt": stdgrosswgt,
                #     "stdnetwgt": stdnetwgt
                # })
                # response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
                # if response.status_code > 200:
                #     if response.status_code == 401:  # unauthorised needs to refresh token
                #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                #         if success:
                #             params = {'access_token': wms_access_token}
                #             response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
                #             if response.status_code > 200:
                #                 raise osv.except_osv(_(response.reason + ' for product ' + sku), _(response.content))
                #         else:
                #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
                #     else:
                #         raise osv.except_osv(_(response.reason + ' for product ' + sku), _(response.content))

            receiptlinenumber += 1
            lottable03 = move.prodlot_id and move.prodlot_id.name or ''

            asn_list.append({
                'storerkey': storerkey,
                'sku': sku and sku[:50] or '',
                'receiptkey': receiptkey,
                'receiptlinenumber': str(receiptlinenumber).zfill(5),
                'addwho': 'wmwhse1',
                'editwho': 'wmwhse1',
                'packkey': 'STD',
                'toloc': 'STAGE',  # 'DOCK'
                'uom': 'EA',
                'whseid': 'wmwhse1',
                'qtyexpected': move.product_qty,
                'qtyreceived': 0,  # move.product_qty if move.state == 'done' else 0,
                # 'lottable03': lottable03,
                'externlineno': move.id,
            })
        asn_dict.update({'receiptdetails': asn_list})
        payload = json.dumps(asn_dict)
        url = wms_api_endpoint + '/' + wms_warehouse_name + '/' + 'advancedshipnotice'
        params = {'access_token': wms_access_token}
        try:
            print "\n\nurl : ", url, "\npayload :\n", payload
            response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
            if response.status_code > 200:
                if response.status_code == 401:  # unauthorised needs to refresh token
                    success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                    if success:
                        params = {'access_token': wms_access_token}
                        response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
                        if response.status_code > 200:
                            raise osv.except_osv(_(response.reason), _(response.content))
                    else:
                        raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')

                else:
                    raise osv.except_osv(_(response.reason), _(response.content))
            if response.status_code == 200:
                self.pool.get('stock.picking').write(cr, uid, [picking.id], {'wms_api_order_id': picking.id})
                self.pool.get('mail.message').create(cr, uid, {
                    'body': '<div><span>Record Exported TO WMS ASN/Receipt Id : ' + receiptkey + '</span></div>',
                    'model': 'stock.picking',
                    'record_name': picking.name,
                    'res_id': picking.id,
                    'type': 'notification',
                }, context=context)
            else:
                raise osv.except_osv(_(response.reason), _(response.content))

        except Exception, e:
            raise osv.except_osv(_('Exception At The Time Of WMS ASN Create/Update'), _(str(e)))

    def wms_api_out_export(self, cr, uid, picking, context=None):
        mrp_obj = self.pool.get('mrp.production')
        inland_empire_id = 21382  # Inland Empire Location ID
        wms_details = self.pool.get('wms.config.settings').default_get(cr, SUPERUSER_ID, [], context=context)
        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')
        storerkey = wms_details.get('wms_store_key')
        owner = wms_details.get('wms_owner')
        wms_warehouse_name = 'ATLASUSA_PRD_ATLASUSA_PRD_SCE_PRD_0_wmwhse1'  # 'ATLASUSA_TST_ATLASUSA_TST_SCE_PRD_0_wmwhse1'

        print "picking Name ", picking.name
        if context.get('from_auto_order'):
            for move in picking.move_lines:
                mrp_ids = mrp_obj.search(cr, uid, [('move_prod_id', '=', move.id)])
                if not mrp_ids:  # Normal Product
                    if move.state in ['draft', 'cancel', 'waiting']:
                        return True
                else:
                    mrp = mrp_obj.browse(cr, uid, mrp_ids[0])
                    if mrp.state in ['draft', 'cancel', 'picking_except']:
                        return True
        else:
            for move in picking.move_lines:
                mrp_ids = mrp_obj.search(cr, uid, [('move_prod_id', '=', move.id)])
                if not mrp_ids:  # Normal Product
                    if move.state in ['draft', 'cancel', 'waiting', 'confirmed']:
                        return True
                else:
                    mrp = mrp_obj.browse(cr, uid, mrp_ids[0])
                    if mrp.state in ['draft', 'cancel', 'picking_except', 'confirmed']:
                        return True

        so = picking.sale_id
        trading_partner = so and so.partner_id.name or picking.partner_id.name or ''
        if trading_partner:
            trading_partner = re.sub(r'[^\w]', ' ', trading_partner)  # Remove Special Symbols
            trading_partner = re.sub(r'\d', ' ', trading_partner)  # Remove Numbers
            trading_partner = trading_partner.replace('  ', ' ')  # Remove Extra Space
            trading_partner = trading_partner.replace('  ', ' ')  # Remove Extra Space
            trading_partner = trading_partner.replace('  ', ' ')  # Remove Extra Space

        orderkey = picking.name.split('/')[1]
        shipments_dict = {
            "orderkey": orderkey,
            "buyerpo": picking.po_ref or '',
            "caddress1": picking.drop_ship_add1 or '',
            "caddress2": picking.drop_ship_add2 or '',
            "caddress3": so and so.dropship_company or '',
            "ccity": picking.drop_shipping_city or '',
            "ccompany": so and so.dropship_contact_person or '',
            "ccountry": so.dropship_country and so.dropship_country.code or "USA",
            "cemail1": picking.drop_shipping_email or '',
            "cphone1": picking.drop_shipping_phone or '',
            "cstate": so.dropship_state and so.dropship_state.code or '',
            "czip": picking.drop_shipping_zip or '',
            "externorderkey": picking.name,
            "storerkey": storerkey,
            "tradingpartner": trading_partner,
            "whseid": 'wmwhse1',
            "orderdetails": []
        }

        counter = 0
        orderdetails = []
        for move in picking.move_lines:
            if move.location_id.id == inland_empire_id:
                mrp_ids = mrp_obj.search(cr, uid, [('move_prod_id', '=', move.id)])
                counter += 1
                if not mrp_ids:  # Normal Product
                    orderlinenumber = "%05d" % counter
                    prod = move.product_id
                    sku = prod.default_code or prod.magento_sku
                    search_item_endpoint = wms_api_endpoint + '/' + wms_warehouse_name + '/items/' + sku + '/' + owner
                    params = {'access_token': wms_access_token}
                    search_item_resp = requests.request("GET", search_item_endpoint, data={}, params=params,
                                                        headers={'Content-Type': 'application/json'})
                    if search_item_resp.status_code == 200:
                        pass
                    elif search_item_resp.status_code == 401:  # unauthorised needs to refresh token
                        success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                        if success:
                            params = {'access_token': wms_access_token}
                            search_item_resp = requests.request("GET", search_item_endpoint, data={}, params=params,
                                                                headers={'Content-Type': 'application/json'})
                            if search_item_resp.status_code == 200:
                                pass
                            else:
                                raise osv.except_osv(_(search_item_resp.reason), _(search_item_resp.content))
                        else:
                            raise osv.except_osv(_("Refresh Token:"), _(search_item_resp.content))
                    else:
                        raise osv.except_osv(_("Data Error"), _("Product with SKU %s is not present in the WMS system" % sku))
                        # # Create A New Product
                        # stdnetwgt = prod.magento_exportable and prod.x_magerp_ship_weight or prod.weight_net or 0
                        # stdgrosswgt = prod.magento_exportable and prod.x_magerp_prod_weight or prod.weight or 0
                        # wh_id = 'ATLASUSA_TST_ENTERPRISE'  # Here we can use only ATLASUSA_TST_ENTERPRISE as a warehouse because it is parent wahrehouse
                        # url = wms_api_endpoint + '/' + wh_id + '/items'
                        # payload = json.dumps({
                        #     "storerkey": storerkey,
                        #     "sku": sku and sku[:50] or '',
                        #     "descr": prod.description and prod.description[:50] or '',  # 254
                        #     "packkey": "STD",
                        #     "stdgrosswgt": stdgrosswgt,
                        #     "stdnetwgt": stdnetwgt
                        # })
                        # response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
                        # if response.status_code > 200:
                        #     if response.status_code == 401:  # unauthorised needs to refresh token
                        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                        #         if success:
                        #             params = {'access_token': wms_access_token}
                        #             response = requests.request("POST", url, data=payload, params=params,
                        #                                         headers={'Content-Type': 'application/json'})
                        #             if response.status_code > 200:
                        #                 raise osv.except_osv(_(response.reason + ' for product ' + sku), _(response.content))
                        #         else:
                        #             raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
                        #     else:
                        #         raise osv.except_osv(_(response.reason + ' for product ' + sku), _(response.content))

                    lottable03 = move.prodlot_id and move.prodlot_id.name or ''
                    orderdetails.append({
                        "orderkey": orderkey,
                        "orderlinenumber": orderlinenumber,
                        "cartongroup": "STD",
                        "externlineno": move.id,
                        "externorderkey": picking.name,
                        "itemclass": "STD",
                        # "lottable01": "string",
                        "openqty": move.product_qty,
                        "packkey": "STD",
                        "sku": sku,
                        "storerkey": storerkey,
                        "uom": "EA",
                        "whseid": 'wmwhse1',
                        # "lottable03": lottable03, # Client Do Not want this
                    })

                else:  # MRP Flow
                    mrp_moves = []
                    mrp = mrp_obj.browse(cr, uid, mrp_ids[0])
                    if mrp.move_lines:
                        for mv in mrp.move_lines:
                            mrp_moves.append(mv.id)
                        # mrp_moves = mrp.move_lines
                    elif mrp.move_lines2:
                        for mv in mrp.move_lines2:
                            mrp_moves.append(mv.id)
                        # mrp_moves = mrp.move_lines2
                    if mrp_moves:
                        for mrp_move in self.pool.get('stock.move').browse(cr, uid, mrp_moves):
                            # if mrp_move.location_id.id == inland_empire_id:
                            orderlinenumber = "%05d" % counter
                            counter += 1
                            prod = mrp_move.product_id
                            sku = prod.default_code or prod.magento_sku
                            search_item_endpoint = wms_api_endpoint + '/' + wms_warehouse_name + '/items/' + sku + '/' + owner
                            params = {'access_token': wms_access_token}
                            search_item_resp = requests.request("GET", search_item_endpoint, data={}, params=params,
                                                                headers={'Content-Type': 'application/json'})
                            if search_item_resp.status_code == 200:
                                pass

                            elif search_item_resp.status_code == 401:  # unauthorised needs to refresh token
                                success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                                if success:
                                    params = {'access_token': wms_access_token}
                                    search_item_resp = requests.request("GET", search_item_endpoint, data={}, params=params,
                                                                        headers={'Content-Type': 'application/json'})
                                    if search_item_resp.status_code == 200:
                                        pass
                                    else:
                                        raise osv.except_osv(_(search_item_resp.reason), _(search_item_resp.content))
                                else:
                                    raise osv.except_osv(_("Refresh Token:"), _(search_item_resp.content))
                            else:
                                raise osv.except_osv(_("Data Error"),
                                                     _("Consumable Product with SKU %s from MRP Order %s is not present in the WMS system" % (
                                                         sku, mrp.name)))

                            lottable03 = mrp_move.prodlot_id and mrp_move.prodlot_id.name or ''
                            orderdetails.append({
                                "orderkey": orderkey,
                                "orderlinenumber": orderlinenumber,
                                "cartongroup": "STD",
                                "externlineno": mrp_move.id,
                                "externorderkey": picking.name,
                                "itemclass": "STD",
                                # "lottable01": "string",
                                "openqty": mrp_move.product_qty,
                                "packkey": "STD",
                                "sku": sku,
                                "storerkey": storerkey,
                                "uom": "EA",
                                "whseid": 'wmwhse1',
                                "lottable03": lottable03,
                            })

        shipments_dict.update({'orderdetails': orderdetails})
        payload = json.dumps(shipments_dict)

        url = wms_api_endpoint + '/' + wms_warehouse_name + '/' + 'shipments'
        params = {'access_token': wms_access_token}
        try:
            print "\n\nurl : ", url, "\npayload :\n", payload
            response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
            if response.status_code > 200:
                if response.status_code == 401:  # unauthorised needs to refresh token
                    success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                    if success:
                        params = {'access_token': wms_access_token}
                        response = requests.request("POST", url, data=payload, params=params, headers={'Content-Type': 'application/json'})
                        if response.status_code > 200:
                            raise osv.except_osv(_(response.reason), _(response.content))
                    else:
                        raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
                else:
                    raise osv.except_osv(_(response.reason), _(response.content))

            if response.status_code == 200:
                self.pool.get('stock.picking').write(cr, uid, [picking.id], {'wms_api_order_id': orderkey})
                self.pool.get('mail.message').create(cr, uid, {
                    'body': '<div><span>Record Exported TO WMS Order Number : ' + orderkey + '</span></div>',
                    'model': 'stock.picking',
                    'record_name': picking.name,
                    'res_id': picking.id,
                    'type': 'notification',
                }, context=context)
            else:
                raise osv.except_osv(_(response.reason), _(response.content))

        except Exception, e:
            raise osv.except_osv(_('Exception At The Time Of WMS Shipment Order Create/Update'), _(str(e)))
        return True

    def process_asn_closed_wms(self, cr, uid, resp, context=None):
        lot_obj = self.pool.get('stock.production.lot')
        move_obj = self.pool.get('stock.move')
        try:
            print "\n\n\n\n\nprocess_asn_closed_wms RESP:\n\n", resp
            for export in resp:
                move_split_dict = {}  # {'move_id':{batch:qty}}
                msg = json.loads(export.get('jsonMessage'))
                print "\n\n\nmsg:\n\n", msg
                key1 = export.get('key1')
                key2 = export.get('key2')
                picking_ids = []
                if key2:
                    picking_ids = self.search(cr, uid, [('name', '=', key2), ('type', '=', 'in')])
                if not picking_ids and key1:
                    all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'in')])
                    if all_picking_ids:
                        for picking in self.browse(cr, uid, all_picking_ids):
                            if picking.name.split('/')[1] == key1:
                                picking_ids = [picking.id]
                                break
                if not picking_ids:
                    extern_receipt_key = msg['AsnClosed']['AsnHeader'].get('ExternReceiptKey')
                    picking_ids = self.search(cr, uid, [('name', '=', extern_receipt_key), ('type', '=', 'in')])

                if picking_ids:
                    picking = self.browse(cr, uid, picking_ids[0])
                    if picking.state == 'done':
                        continue
                    partial_datas = {'delivery_date': fields.datetime.now()}

                    asndetail = msg['AsnClosed']['AsnHeader'].get('AsnDetail')
                    if isinstance(asndetail, dict):
                        asn_details = [asndetail]
                    elif isinstance(asndetail, list):
                        asn_details = asndetail

                    for asn in asn_details:
                        move_id = None
                        received_qty = float(asn.get('QtyReceived'))
                        if not received_qty:
                            continue
                        try:
                            move_id = move_obj.search(cr, uid, [('id', '=', int(asn.get('ExternLineNo'))), ('picking_id', '=', picking_ids[0])])
                            if not move_id:
                                move_id = move_obj.search(cr, uid, [('product_id.default_code', '=', asn.get('Sku')),
                                                                    ('picking_id', '=', picking_ids[0])])
                        except Exception, e:
                            # Catch If incorrect ExternLineNo (it means It is additional product and We have created extra move in pinking)
                            pass
                        if move_id:
                            serial_number = None
                            move_id = move_id[0]
                            move = move_obj.browse(cr, uid, move_id)
                            if asn.get('Lottable03'):  # Select or generate serial number if required
                                serial_number_ids = lot_obj.search(cr, uid, [('name', '=', asn.get('Lottable03')),
                                                                             ('product_id', '=', move.product_id.id)])
                                if serial_number_ids:
                                    serial_number = serial_number_ids[0]
                                else:
                                    serial_number = lot_obj.create(cr, uid, {
                                        'name': asn.get('Lottable03'),
                                        'product_id': move.product_id.id,
                                        'date': fields.datetime.now(),
                                    })
                                if move_id in move_split_dict:
                                    inside_dict = move_split_dict[move_id]
                                    if serial_number in inside_dict:
                                        inside_dict.update({serial_number: inside_dict[serial_number] + received_qty})
                                        move_split_dict.update({move_id: inside_dict})
                                    else:
                                        inside_dict.update({serial_number: received_qty})
                                        move_split_dict.update({move_id: inside_dict})
                                else:
                                    move_split_dict.update({move_id: {serial_number: received_qty}})
                                # move_obj.write(cr, uid, [move.id], {'prodlot_id': serial_number})
                            else:
                                if move_id in move_split_dict:
                                    inside_dict = move_split_dict[move_id]
                                    if 'blank' in inside_dict:
                                        inside_dict.update({'blank': inside_dict['blank'] + received_qty})
                                        move_split_dict.update({move_id: inside_dict})
                                    else:
                                        inside_dict.update({'blank': received_qty})
                                        move_split_dict.update({move_id: inside_dict})
                                else:
                                    move_split_dict.update({move_id: {'blank': received_qty}})

                        else:
                            product_ids = self.pool.get('product.product').search(cr, uid, [('default_code', '=', asn.get('Sku'))])
                            if product_ids:
                                prod = self.pool.get('product.product').browse(cr, uid, product_ids[0])
                                mod, supplier_location_id = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'stock',
                                                                                                                'stock_location_suppliers')
                                name = self.pool.get('product.product').name_get(cr, uid, product_ids[0], context=context)[0][1]

                                serial_number = None
                                if asn.get('Lottable03'):  # Select or generate serial number if required
                                    serial_number_ids = lot_obj.search(cr, uid, [('name', '=', asn.get('Lottable03')), ('product_id', '=', prod.id)])
                                    if serial_number_ids:
                                        serial_number = serial_number_ids[0]
                                    else:
                                        serial_number = lot_obj.create(cr, uid, {'name': asn.get('Lottable03'), 'product_id': prod.id,
                                                                                 'date': fields.datetime.now()})
                                move_dict = {
                                    'name': name,
                                    'product_id': product_ids[0],
                                    'product_qty': received_qty,
                                    'product_uos_qty': received_qty,
                                    'product_uom': prod.uom_id.id,
                                    'product_uos': prod.uom_id.id,
                                    'location_id': supplier_location_id,
                                    'location_dest_id': 21382,  # Inland Empire Location ID
                                    'picking_id': picking_ids[0],
                                    'partner_id': picking.partner_id.id,
                                    # 'wms_order_id': wms_id,
                                    'prodlot_id': serial_number,
                                    'origin': 'WMS IN IMPORT',
                                    'type': 'in',
                                    'company_id': picking.company_id.id,
                                    'price_unit': 0.0,  # It may be complimentary or free product so we set price unit to zero
                                }
                                move_id = move_obj.create(cr, uid, move_dict)
                                move_obj.action_confirm(cr, uid, [move_id])
                                move_obj.action_assign(cr, uid, [move_id])

                                move = move_obj.browse(cr, uid, move_id)

                                if serial_number:
                                    move_split_dict.update({move_id: {serial_number: received_qty}})
                                else:
                                    move_split_dict.update({move_id: {'blank': received_qty}})

                    if move_split_dict:
                        for move_id, inside_dict in move_split_dict.items():
                            if len(inside_dict) > 1:
                                # TODO:split Move
                                serial_dict = inside_dict.copy()
                                try:
                                    del serial_dict['blank']
                                except Exception, e:
                                    pass
                                success, error = self.split_move_prodlot_id(cr, uid, move_id, serial_dict, context=context)
                                if success:
                                    success.append(move_id)
                                    for mv in success:
                                        move_obj.action_confirm(cr, uid, [mv])
                                        move_obj.action_assign(cr, uid, [mv])

                                    for serial, qty in serial_dict.items():
                                        splitted_move_id = move_obj.search(cr, uid, [('prodlot_id', '=', serial), ('id', 'in', success)])
                                        if splitted_move_id:
                                            partial_datas['move%s' % splitted_move_id[0]] = {
                                                'product_id': move.product_id.id,
                                                'product_qty': qty,
                                                'product_uom': move.product_uom.id,
                                                'prodlot_id': serial,
                                            }
                                if 'blank' in inside_dict:
                                    partial_datas['move%s' % move_id] = {
                                        'product_id': move.product_id.id,
                                        'product_qty': inside_dict['blank'],
                                        'product_uom': move.product_uom.id,
                                    }
                            else:
                                partial_datas['move%s' % move_id] = {
                                    'product_id': move.product_id.id,
                                    'product_qty': float(inside_dict.values()[0]),
                                    'product_uom': move.product_uom.id,
                                    'prodlot_id': inside_dict.keys()[0],
                                }
                    print "\npartial_datas :\n", partial_datas
                    if partial_datas:
                        self.write(cr, uid, picking_ids, {'date_done': fields.datetime.now()})
                        self.do_partial(cr, uid, picking_ids, partial_datas, context=context)
                    else:
                        return 'No Moves Found'
                else:
                    return 'No Picking Found'
        except Exception, e:
            return 'Error while processing ASN ' + str(e)

    def asn_closed_wms(self, cr, uid, wms_details, warehouse_name, context=None):
        skipped_response = self.pool.get('wms.skipped.response')
        # First try to process pending response
        skipped_resp_ids = skipped_response.search(cr, uid, [('method', '=', 'ASNCLOSED')])
        for skipped_resp in skipped_response.browse(cr, uid, skipped_resp_ids):
            error_resp = self.process_asn_closed_wms(cr, uid, json.loads(skipped_resp.response))
            if error_resp:
                skipped_response.write(cr, uid, [skipped_resp.id], {'last_message': error_resp})
            else:
                skipped_response.unlink(cr, uid, [skipped_resp.id])

        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')
        url = wms_api_endpoint + '/' + warehouse_name + '/' + 'exports'
        params = {'access_token': wms_access_token}

        asnclosed_dict = {
            'updatestatus': '9',
            'transmitflagtouse': 'transmitflag5',
            'eventcategory': 'M',
            'types': ['ASNCLOSED'],
            'restrictrowsto': 15,
            'asJson': True,
            'generatemessages': True,
            'rebuildmessages': False,
            'ignoredepositcatchdata': False
        }
        response = requests.request("POST", url, data=json.dumps(asnclosed_dict), params=params, headers={'Content-Type': 'application/json'})

        if response.status_code > 200:
            if response.status_code == 401:  # unauthorised needs to refresh token
                success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                if success:
                    params = {'access_token': wms_access_token}
                    response = requests.request("POST", url, data=json.dumps(asnclosed_dict), params=params,
                                                headers={'Content-Type': 'application/json'})
                    if response.status_code > 200:
                        raise osv.except_osv(_(response.reason), _(response.content))
                else:
                    raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
            else:
                raise osv.except_osv(_(response.reason), _(response.content))

        error_resp = self.process_asn_closed_wms(cr, uid, json.loads(response.text))
        if error_resp:
            skipped_response.create(cr, uid, {'method': 'ASNCLOSED', 'response': response.text, 'last_message': error_resp})

    def process_order_shipped_wms(self, cr, uid, resp, context=None):
        if not context:
            context = {}
        move_obj = self.pool.get('stock.move')
        try:
            print "\n\n\nprocess_order_shipped_wms resp :\n", resp
            for export in resp:
                picking_ids = []
                duplicate_move_ids = []
                duplicate_move = {}
                record_dict = {1: {}, 0: []}  # {mrp/non_mrp(1/0):{(For MRP){mrp_id:[line]},(Foe Non MRP[lines])}

                key1 = export.get('key1')
                if key1:
                    all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'out')])
                    if all_picking_ids:
                        for picking in self.browse(cr, uid, all_picking_ids):
                            if picking.name.split('/')[1] == key1:
                                picking_ids = [picking.id]
                                break
                if picking_ids:
                    picking = self.browse(cr, uid, picking_ids[0])
                    if picking.state == 'done':
                        continue
                    partial_datas = {'delivery_date': fields.datetime.now()}
                    msg = json.loads(export.get('jsonMessage'))
                    shipment_order_detail = msg['ShipmentConfirmation']['ShipmentOrderHeader'].get('ShipmentOrderDetail')
                    if isinstance(shipment_order_detail, dict):
                        shipment_order_details = [shipment_order_detail]
                    elif isinstance(shipment_order_detail, list):
                        shipment_order_details = shipment_order_detail
                    print "\n\n\nshipment_order_details:\n\n", shipment_order_details
                    for shipment_order in shipment_order_details:
                        product_qty = float(shipment_order.get('ShippedQty', 0))
                        move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(shipment_order.get('ExternLineNo')))])
                        if not move_id:
                            move_id = self.pool.get('stock.move').search(cr, uid, [('product_id.default_code', '=', shipment_order.get('Sku')),
                                                                                   ('picking_id', '=', picking_ids[0])])
                        if move_id:
                            serial_number = None
                            move = self.pool.get('stock.move').browse(cr, uid, move_id[0])
                            if shipment_order.get('Lottable03'):  # Select or generate serial number if required
                                lot_obj = self.pool.get('stock.production.lot')
                                serial_number_ids = lot_obj.search(cr, uid, [('name', '=', shipment_order.get('Lottable03')),
                                                                             ('product_id', '=', move.product_id.id)])
                                if serial_number_ids:
                                    serial_number = serial_number_ids[0]
                                else:
                                    serial_number = lot_obj.create(cr, uid, {
                                        'name': shipment_order.get('Lottable03'),
                                        'product_id': move.product_id.id,
                                        'date': fields.datetime.now(),
                                    })
                                # if serial_number:
                                #     serial_number = shipment_order.get('Lottable03')

                            shipment_order.update({'move': move, 'serial_number': serial_number})
                            mrp_id = self.pool.get('stock.move')._get_mrp_from_move(cr, uid, move_id[0])
                            print "\nmrp_id : ----------------------", mrp_id
                            if mrp_id:
                                if record_dict[1].get(mrp_id):
                                    # Add line into Existing MRP sub dict
                                    content_list = record_dict[1][mrp_id]
                                    content_list.append(shipment_order)
                                    record_dict[1].update({mrp_id: content_list})
                                else:
                                    # Add entry of new mrp line
                                    record_dict[1].update({mrp_id: [shipment_order]})
                            else:
                                record_dict[0].append(shipment_order)  # Normal Line

                            # Duplicate Move Id To Split
                            print "\nduplicate_move :\n", duplicate_move, "\nmove_id : ", move_id
                            if duplicate_move.get(move_id[0]):
                                serial_dict = duplicate_move.get(move_id[0])
                                if serial_number:
                                    if serial_dict.get(serial_number):
                                        # If multiple lines with same location move and serial number
                                        existing_qty = serial_dict[serial_number]
                                        serial_dict.update({serial_number: existing_qty + product_qty})
                                    else:
                                        serial_dict.update({serial_number: product_qty})
                                duplicate_move.update({move_id[0]: serial_dict})
                            else:
                                duplicate_move.update({move_id[0]: {serial_number: product_qty}})

                    for move_id, serial_dict in duplicate_move.items():
                        if len(serial_dict) > 1:
                            duplicate_move_ids.append(move_id)
                            move_val = {'wms_order_id': shipment_order.get('OrderKey')}
                            if not context.get('old_import'):
                                factory_serial_number = ''
                                if shipment_order.get('LotxIDDetail'):
                                    LotxIDDetail = shipment_order.get('LotxIDDetail', [])
                                    lotxdtl_list = []
                                    if isinstance(LotxIDDetail, dict):
                                        lotxdtl_list = [LotxIDDetail]
                                    elif isinstance(LotxIDDetail, list):
                                        lotxdtl_list = LotxIDDetail

                                    for lotxdtl in lotxdtl_list:
                                        if lotxdtl.get('OOther1'):
                                            if factory_serial_number:
                                                factory_serial_number = factory_serial_number + ';' + lotxdtl.get('OOther1')
                                            else:
                                                factory_serial_number = lotxdtl.get('OOther1')
                                    move_val.update({'factory_serial_number': factory_serial_number})
                            self.pool.get('stock.move').write(cr, uid, [move_id], move_val)
                            success, error = self.split_move_prodlot_id(cr, uid, move_id, serial_dict, context=context)
                            if error:
                                return success, error

                    print "record_dict : ", record_dict

                    # For MRP Moves
                    if record_dict[1]:
                        # self.check_duplicate_move(cr, uid, record_list, context=context)
                        # Set New Location, Serial Number and WMS Id And Check Product is available or not if available change state to Ready To Available
                        mrp = None
                        mrp_obj = self.pool.get('mrp.production')
                        for mrp_id, shipment_order_list in record_dict[1].items():
                            mrp = mrp_obj.browse(cr, uid, mrp_id)
                            for shipment_order in shipment_order_list:
                                move = shipment_order.get('move')
                                move_dict = {}
                                factory_serial_number = ''
                                if not context.get('old_import'):

                                    LotxIDDetail = shipment_order.get('LotxIDDetail', [])
                                    lotxdtl_list = []
                                    if isinstance(LotxIDDetail, dict):
                                        lotxdtl_list = [LotxIDDetail]
                                    elif isinstance(LotxIDDetail, list):
                                        lotxdtl_list = LotxIDDetail

                                    for lotxdtl in lotxdtl_list:
                                        if lotxdtl.get('OOther1'):
                                            if factory_serial_number:
                                                factory_serial_number = factory_serial_number + ';' + lotxdtl.get('OOther1')
                                            else:
                                                factory_serial_number = lotxdtl.get('OOther1')
                                        move_dict.update({'factory_serial_number': factory_serial_number})

                                if factory_serial_number:
                                    self.pool.get('stock.move').write(cr, uid, [move.id], {'factory_serial_number': factory_serial_number})

                                creation_date = shipment_order.get('AddDate')
                                picked_date = shipment_order.get('ActualShipDate')
                                wms_id = shipment_order.get('OrderKey')
                                if creation_date:
                                    creation_date = datetime.strptime(creation_date, '%m/%d/%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
                                if picked_date:
                                    picked_date = datetime.strptime(picked_date, '%m/%d/%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')

                                move_dict.update({'prodlot_id': shipment_order.get('serial_number')})
                                if wms_id:
                                    move_dict.update({'wms_order_id': wms_id})

                                sale_order, picking = mrp_obj._get_sale_order_picking(cr, uid, [mrp_id])
                                self.pool.get('stock.picking.out').write(cr, uid, [picking.id], {
                                    'wms_receive_date': creation_date,
                                    'wms_pick_date': picked_date,
                                    'wms_order_id': wms_id,
                                })
                                print "duplicate_move_ids---------------------- : ", duplicate_move_ids
                                if not move.id in duplicate_move_ids:
                                    cons_move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(shipment_order.get('ExternLineNo')))])
                                    if cons_move_id:
                                        move_obj.write(cr, uid, cons_move_id, move_dict)

                                        if shipment_order.get('serial_number'):  # Set Serial Number To internal Move Too
                                            internal_move_ids = move_obj.search(cr, uid, [('move_dest_id', '=', cons_move_id[0])])
                                            if internal_move_ids:
                                                move_obj.write(cr, uid, internal_move_ids, {'prodlot_id': shipment_order.get('serial_number')})

                                if mrp.state == 'ready':
                                    # If material is ready then create final Product
                                    if context:
                                        ctx = context.copy()
                                        ctx.update({'active_id': mrp.id, 'active_model': 'mrp.production'})
                                    else:
                                        ctx = {'active_id': mrp.id, 'active_model': 'mrp.production'}
                                    product_qty = self.pool.get('mrp.product.produce')._get_product_qty(cr, uid, context=ctx)
                                    mrp_obj.action_produce(cr, uid, mrp.id, product_qty, 'consume_produce', context=ctx)

                    #######################################################################################################
                    # Normal Stock Move
                    if record_dict[0]:
                        for shipment_order in record_dict[0]:
                            move_dict = {}
                            move = shipment_order.get('move')
                            factory_serial_number = ''
                            serial_number = shipment_order.get('serial_number')
                            wms_id = shipment_order.get('OrderKey')
                            creation_date = shipment_order.get('AddDate')
                            picked_date = shipment_order.get('ActualShipDate')
                            if creation_date:
                                creation_date = datetime.strptime(creation_date, '%m/%d/%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
                            if picked_date:
                                picked_date = datetime.strptime(picked_date, '%m/%d/%Y %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
                            if shipment_order.get('LotxIDDetail'):
                                LotxIDDetail = shipment_order.get('LotxIDDetail', [])
                                lotxdtl_list = []
                                if isinstance(LotxIDDetail, dict):
                                    lotxdtl_list = [LotxIDDetail]
                                elif isinstance(LotxIDDetail, list):
                                    lotxdtl_list = LotxIDDetail

                                for lotxdtl in lotxdtl_list:
                                    if lotxdtl.get('OOther1'):
                                        if factory_serial_number:
                                            factory_serial_number = factory_serial_number + ';' + lotxdtl.get('OOther1')
                                        else:
                                            factory_serial_number = lotxdtl.get('OOther1')

                            if factory_serial_number:
                                move_dict.update({'factory_serial_number': factory_serial_number})
                            if serial_number:
                                move_dict.update({'prodlot_id': serial_number})
                            if wms_id:
                                move_dict.update({'wms_order_id': wms_id})
                            move_obj.write(cr, uid, [move.id], move_dict)

                            partial_datas['move%s' % move.id] = {
                                'product_id': move.product_id.id,
                                'product_qty': float(shipment_order.get('ShippedQty', 0)),
                                'product_uom': move.product_uom.id,
                                'prodlot_id': serial_number,
                            }
                        if partial_datas:
                            print "\n\npartial_datas :\n", partial_datas
                            self.write(cr, uid, picking_ids, {
                                'wms_receive_date': creation_date,
                                'wms_pick_date': picked_date,
                                'wms_order_id': wms_id
                            })
                            self.do_partial(cr, uid, picking_ids, partial_datas, context)
                        else:
                            return 'No Moves Found'
                else:
                    return 'No Picking Found'
        except Exception, e:
            return 'Error while processing WMS ORDERSHIPPED ' + str(e)
            # raise osv.except_osv(_('Exception At The Time Of WMS ORDERSHIPPED Create/Update'), _(str(e)))
        return False

    def order_shipped_wms(self, cr, uid, wms_details, warehouse_name, context=None):
        skipped_response = self.pool.get('wms.skipped.response')
        # First try to process pending response
        skipped_resp_ids = skipped_response.search(cr, uid, [('method', '=', 'ORDERSHIPPED')])
        for skipped_resp in skipped_response.browse(cr, uid, skipped_resp_ids):
            error_resp = self.process_order_shipped_wms(cr, uid, json.loads(skipped_resp.response), context=context)
            if error_resp:
                skipped_response.write(cr, uid, [skipped_resp.id], {'last_message': error_resp})
            else:
                skipped_response.unlink(cr, uid, [skipped_resp.id])

        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')
        url = wms_api_endpoint + '/' + warehouse_name + '/' + 'exports'
        params = {'access_token': wms_access_token}

        reshipped_dict = {
            'updatestatus': '9',
            'transmitflagtouse': 'transmitflag5',
            'eventcategory': 'M',
            'types': ['ORDERSHIPPED'],
            'restrictrowsto': 15,
            'asJson': True,
            'generatemessages': True,
            'rebuildmessages': False,
            'ignoredepositcatchdata': False
        }
        response = requests.request("POST", url, data=json.dumps(reshipped_dict), params=params, headers={'Content-Type': 'application/json'})
        if response.status_code > 200:
            if response.status_code == 401:  # unauthorised needs to refresh token
                success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                if success:
                    params = {'access_token': wms_access_token}
                    response = requests.request("POST", url, data=json.dumps(reshipped_dict), params=params,
                                                headers={'Content-Type': 'application/json'})
                    if response.status_code > 200:
                        raise osv.except_osv(_(response.reason), _(response.content))
                else:
                    raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
            else:
                raise osv.except_osv(_(response.reason), _(response.content))

        error_resp = self.process_order_shipped_wms(cr, uid, json.loads(response.text), context=context)
        if error_resp:
            skipped_response.create(cr, uid, {'method': 'ORDERSHIPPED', 'response': response.text, 'last_message': error_resp})

    def process_transfer_finalized_wms(self, cr, uid, resp, wms_details, warehouse_name, context=None):
        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')

        try:
            for export in resp:
                msg = json.loads(export.get('jsonMessage'))
                print "\n\n\nexport msg :\n", msg
                transfer_key = msg['TransferConfirmation']['TransferConfirmationHeader'].get('TransferKey')
                if transfer_key and not self.pool.get('stock.picking').search(cr, uid, [('wms_api_order_id', '=', transfer_key)]):
                    # Fetch internal transfer details from WMS
                    transfer_url = wms_api_endpoint + '/' + warehouse_name + '/transfers/' + transfer_key
                    transfer_resp = requests.request("GET", transfer_url, data={}, params={'access_token': wms_access_token},
                                                     headers={'Content-Type': 'application/json'})

                    if transfer_resp.status_code > 200:
                        if transfer_resp.status_code == 401:  # unauthorised needs to refresh token
                            success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                            if success:
                                transfer_resp = requests.request("GET", transfer_url, data={}, params={'access_token': wms_access_token},
                                                                 headers={'Content-Type': 'application/json'})
                                if transfer_resp.status_code > 200:
                                    raise osv.except_osv(_(transfer_resp.reason), _(transfer_resp.content))
                            else:
                                raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
                        else:
                            raise osv.except_osv(_(transfer_resp.reason), _(transfer_resp.content))

                    if transfer_resp.status_code == 200:
                        picking_obj = self.pool.get('stock.picking')
                        print "\n\n\n\ntransfer_resp resp:\n\n", transfer_resp.text
                        transfer = json.loads(transfer_resp.text)
                        wf_service = netsvc.LocalService("workflow")
                        wms_order_id = transfer.get('transferkey', '')

                        # Create Internal Moves first and add all the move lines to it
                        move_lines = []
                        for transfer_details in transfer.get('transferdetails'):
                            print "transfer_details : ", transfer_details
                            sku = transfer_details.get('fromsku')
                            if sku:
                                product_ids = self.pool.get('product.product').search(cr, uid, [('default_code', '=ilike', sku)])
                            dest_location = transfer_details.get('toloc')
                            if dest_location:
                                if dest_location in ['Damage', 'damage', 'RMA', 'rma']:
                                    dest_location_ids = self.pool.get('stock.location').search(cr, uid, [('name', '=ilike', dest_location)])
                                else:
                                    raise osv.except_osv(_("Problem in Internal Transfer"),
                                                         'For Internal Transfer Dest Location %s Is Not Found' % dest_location)

                            # src_location = transfer_details.get('fromloc')
                            # if src_location:
                            #     src_location_ids = self.pool.get('stock.location').search(cr, uid, [('name', '=ilike', src_location)])
                            src_location_ids = 21382  # Inland Empire Location ID

                            if product_ids and dest_location_ids and src_location_ids:
                                prod = self.pool.get('product.product').browse(cr, uid, product_ids[0])
                                name = self.pool.get('product.product').name_get(cr, uid, product_ids[0], context=context)[0][1]
                                effectivedate = transfer_details.get('effectivedate', '')
                                if effectivedate:
                                    effectivedate = effectivedate.replace('T', ' ')
                                    effectivedate = effectivedate[:19]

                                qty = transfer_details.get('toqty', 0)
                                serial_number = transfer_details.get('lottable03')
                                if serial_number:
                                    serial_number = self.pool.get('stock.production.lot').search(cr, uid, [('name', '=', serial_number),
                                                                                                           ('product_id', '=', product_ids[0])])
                                    if serial_number:
                                        serial_number = serial_number[0]
                                        # # Check Quantity of that serial number is present or not, if not then set none
                                        # move_obj = self.pool.get('stock.move')
                                        # lot_reply = move_obj.onchange_lot_id(cr, uid, [], prodlot_id=serial_number, product_qty=qty,
                                        #                                      loc_id=src_location_ids, product_id=product_ids[0],
                                        #                                      uom_id=prod.uom_id.id)
                                        # if lot_reply:
                                        #     _logger.info(str(lot_reply['title']) + ' : ' + str(lot_reply['message']))
                                        #     serial_number = None
                                    else:
                                        serial_number = None
                                move_dict = {
                                    'name': name,
                                    'product_id': product_ids[0],
                                    'product_qty': qty,
                                    'product_uos_qty': qty,
                                    'product_uom': prod.uom_id.id,
                                    'product_uos': prod.uom_id.id,
                                    'location_id': src_location_ids,
                                    'location_dest_id': dest_location_ids[0],
                                    'wms_order_id': wms_order_id,
                                    'prodlot_id': serial_number,
                                    'origin': 'WMS IN IMPORT API ' + wms_order_id,
                                    'type': 'internal',
                                    'date_expected': effectivedate,
                                }
                                move_lines.append((0, 0, move_dict))
                        print "\n\nmove_lines :\n", move_lines
                        if move_lines:
                            new_picking_id = picking_obj.create(cr, uid, {
                                'type': 'internal',
                                'origin': wms_order_id,
                                'move_lines': move_lines,
                                'wms_api_order_id': wms_order_id,
                                'note': 'Created form WMS API Import Scheduler Import WMS ID : ' + str(wms_order_id),
                            }, context=context)
                            new_picking = picking_obj.browse(cr, uid, new_picking_id, context=context)
                            print "new_picking : ", new_picking
                            for line in new_picking.move_lines:
                                print "line : ", line

                            picking_obj.draft_force_assign(cr, uid, [new_picking_id])
                            if new_picking.state == 'confirmed':
                                if picking_obj.action_assign(cr, uid, [new_picking_id]):
                                    print "YYYYYYYYYYYYYYYYYYYYY"
                                    picking_obj.action_process(cr, uid, [new_picking_id], context=context)
                                    partial_datas = {'delivery_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                                    # partial_datas = {'delivery_date': receive_date}
                                    for move in new_picking.move_lines:
                                        if move.state == 'assigned':
                                            partial_datas['move%s' % move.id] = {
                                                'product_id': move.product_id.id,
                                                'product_qty': move.product_qty,
                                                'product_uom': move.product_uom.id,
                                                'prodlot_id': move.prodlot_id.id,
                                            }
                                    picking_obj.do_partial(cr, uid, [new_picking.id], partial_datas, context)
                                wf_service.trg_write(uid, 'stock.picking', new_picking.id, cr)

        except Exception, e:
            return 'Error while processing WMS TRANSFERFINALIZED ' + str(e)
            # raise osv.except_osv(_('Exception At The Time Of WMS TRANSFERFINALIZED Create/Update'), _(str(e)))

    def transfer_finalized_wms(self, cr, uid, wms_details, warehouse_name, context=None):
        skipped_response = self.pool.get('wms.skipped.response')
        # First try to process pending response
        skipped_resp_ids = skipped_response.search(cr, uid, [('method', '=', 'TRANSFERFINALIZED')])
        for skipped_resp in skipped_response.browse(cr, uid, skipped_resp_ids):
            error_resp = self.process_transfer_finalized_wms(cr, uid, json.loads(skipped_resp.response), wms_details, warehouse_name, context=context)
            if error_resp:
                skipped_response.write(cr, uid, [skipped_resp.id], {'last_message': error_resp})
            else:
                skipped_response.unlink(cr, uid, [skipped_resp.id])

        wms_api_endpoint = wms_details.get('wms_api_endpoint')
        wms_access_token = wms_details.get('wms_access_token')
        url = wms_api_endpoint + '/' + warehouse_name + '/' + 'exports'
        params = {'access_token': wms_access_token}

        ordershipped_dict = {
            'updatestatus': '9',
            'transmitflagtouse': 'transmitflag5',
            'eventcategory': 'M',
            'types': ['TRANSFERFINALIZED'],
            'restrictrowsto': 15,
            'asJson': True,
            'generatemessages': True,
            'rebuildmessages': False,
            'ignoredepositcatchdata': False
        }
        response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params, headers={'Content-Type': 'application/json'})

        if response.status_code > 200:
            if response.status_code == 401:  # unauthorised needs to refresh token
                success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
                if success:
                    params = {'access_token': wms_access_token}
                    response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params,
                                                headers={'Content-Type': 'application/json'})
                    if response.status_code > 200:
                        raise osv.except_osv(_(response.reason), _(response.content))
                else:
                    raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
            else:
                raise osv.except_osv(_(response.reason), _(response.content))

        error_resp = self.process_transfer_finalized_wms(cr, uid, json.loads(response.text), wms_details, warehouse_name, context=context)
        if error_resp:
            skipped_response.create(cr, uid, {'method': 'TRANSFERFINALIZED', 'response': response.text, 'last_message': error_resp})
        return True

    def cron_download_wms_update(self, cr, uid, context=None):
        wms_details = self.pool.get('wms.config.settings').default_get(cr, SUPERUSER_ID, [], context=context)
        # wms_api_endpoint = wms_details.get('wms_api_endpoint')
        # wms_access_token = wms_details.get('wms_access_token')
        wms_warehouse_name = 'ATLASUSA_PRD_ATLASUSA_PRD_SCE_PRD_0_wmwhse1'  # 'ATLASUSA_TST_ATLASUSA_TST_SCE_PRD_0_wmwhse1'

        self.asn_closed_wms(cr, uid, wms_details, wms_warehouse_name, context=context)
        self.order_shipped_wms(cr, uid, wms_details, wms_warehouse_name, context=context)
        self.transfer_finalized_wms(cr, uid, wms_details, wms_warehouse_name, context=context)

        # url = wms_api_endpoint + '/' + wms_warehouse_name + '/' + 'exports'
        # params = {'access_token': wms_access_token}
        #
        # # INCOMING Shipment
        # asnclosed_dict = {
        #     'updatestatus': '9',
        #     'transmitflagtouse': 'transmitflag5',
        #     'eventcategory': 'M',
        #     'types': ['ASNCLOSED'],  # PARTIALSHIPMENT
        #     'restrictrowsto': 15,
        #     'asJson': True,
        #     'generatemessages': True,
        #     'rebuildmessages': False,
        #     'ignoredepositcatchdata': False
        # }
        # response = requests.request("POST", url, data=json.dumps(asnclosed_dict), params=params, headers={'Content-Type': 'application/json'})
        #
        # if response.status_code > 200:
        #     if response.status_code == 401:  # unauthorised needs to refresh token
        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #         if success:
        #             params = {'access_token': wms_access_token}
        #             response = requests.request("POST", url, data=json.dumps(asnclosed_dict), params=params,
        #                                         headers={'Content-Type': 'application/json'})
        #             if response.status_code > 200:
        #                 raise osv.except_osv(_(response.reason), _(response.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #     else:
        #         raise osv.except_osv(_(response.reason), _(response.content))
        #
        # # print "response.text : ", response.text
        # for export in json.loads(response.text):
        #     print "\n\n\nexport:\n", export
        #     key1 = export.get('key1')
        #     key2 = export.get('key2')
        #     picking_ids = []
        #     if key2:
        #         picking_ids = self.search(cr, uid, [('name', '=', key2), ('type', '=', 'in')])
        #     if not picking_ids and key1:
        #         all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'in')])
        #         print "all_picking_ids : ", all_picking_ids
        #         if all_picking_ids:
        #             for picking in self.browse(cr, uid, all_picking_ids):
        #                 if picking.name.split('/')[1] == key1:
        #                     picking_ids = [picking.id]
        #                     break
        #     if picking_ids:
        #         picking = self.browse(cr, uid, picking_ids[0])
        #         if picking.state == 'done':
        #             continue
        #         partial_datas = {'delivery_date': fields.datetime.now()}
        #         msg = json.loads(export.get('jsonMessage'))
        #         print "\n\n\nmsg:\n\n", msg
        #         asndetail = msg['AsnClosed']['AsnHeader'].get('AsnDetail')
        #         if isinstance(asndetail, dict):
        #             asn_details = [asndetail]
        #         elif isinstance(asndetail, list):
        #             asn_details = asndetail
        #
        #         for asn in asn_details:
        #             move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(asn.get('ExternLineNo'))),
        #                                                                    ('picking_id', '=', picking_ids[0])])
        #             if not move_id:
        #                 move_id = self.pool.get('stock.move').search(cr, uid, [('product_id.default_code', '=', asn.get('Sku')),
        #                                                                        ('picking_id', '=', picking_ids[0])])
        #             if move_id:
        #                 serial_number = None
        #                 move = self.pool.get('stock.move').browse(cr, uid, move_id[0])
        #                 print "\n\n\n\nasn.get('Lottable03') :", asn.get('Lottable03')
        #                 if asn.get('Lottable03'):  # Select or generate serial number if required
        #                     lot_obj = self.pool.get('stock.production.lot')
        #                     serial_number_ids = lot_obj.search(cr, uid,
        #                                                        [('name', '=', asn.get('Lottable03')), ('product_id', '=', move.product_id.id)])
        #                     if serial_number_ids:
        #                         serial_number = serial_number_ids[0]
        #                     else:
        #                         serial_number = lot_obj.create(cr, uid, {
        #                             'name': asn.get('Lottable03'),
        #                             'product_id': move.product_id.id,
        #                             'date': fields.datetime.now(),
        #                         })
        #                     move_id = self.pool.get('stock.move').write(cr, uid, [move.id], {'prodlot_id': serial_number})
        #             else:
        #                 location_dest_id = 21382  #Inland Empire Location ID
        #                 product_ids = self.pool.get('product.product').search(cr, uid, [('default_code', '=', asn.get('Sku'))])
        #                 if product_ids:
        #                     prod = self.pool.get('product.product').browse(cr, uid, product_ids[0])
        #                     mod, supplier_location_id = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'stock',
        #                                                                                                     'stock_location_suppliers')
        #                     name = self.pool.get('product.product').name_get(cr, uid, product_ids[0], context=context)[0][1]
        #                     move_dict = {
        #                         'name': name,
        #                         'product_id': product_ids[0],
        #                         'product_qty': float(asn.get('QtyReceived')),
        #                         'product_uos_qty': float(asn.get('QtyReceived')),
        #                         'product_uom': prod.uom_id.id,
        #                         'product_uos': prod.uom_id.id,
        #                         'location_id': supplier_location_id,
        #                         'location_dest_id': location_dest_id,
        #                         'picking_id': picking_ids[0],
        #                         'partner_id': picking.partner_id.id,
        #                         # 'wms_order_id': wms_id,
        #                         'prodlot_id': serial_number,
        #                         'origin': 'WMS IN IMPORT',
        #                         'type': 'in',
        #                         'company_id': picking.company_id.id,
        #                         'price_unit': 0.0,  # It may be complimentary or free product so we set price unit to zero
        #                     }
        #                     move_id = self.pool.get('stock.move').create(cr, uid, move_dict)
        #                     self.pool.get('stock.move').action_confirm(cr, uid, [move_id])
        #                     move = self.pool.get('stock.move').browse(cr, uid, move_id)
        #
        #             partial_datas['move%s' % move.id] = {
        #                 'product_id': move.product_id.id,
        #                 'product_qty': float(asn.get('QtyReceived')),
        #                 'product_uom': move.product_uom.id,
        #                 'prodlot_id': serial_number,
        #             }
        #         print "partial_datas : ", partial_datas
        #         if partial_datas:
        #             self.write(cr, uid, picking_ids, {'date_done': fields.datetime.now()})
        #             print "\n\npicking_ids : ", picking_ids
        #             self.do_partial(cr, uid, picking_ids, partial_datas, context=context)

        # # ORDERSHIPPED
        # ordershipped_dict = {
        #     'updatestatus': '9',
        #     'transmitflagtouse': 'transmitflag5',
        #     'eventcategory': 'M',
        #     'types': ['ORDERSHIPPED'],
        #     'restrictrowsto': 15,
        #     'asJson': True,
        #     'generatemessages': True,
        #     'rebuildmessages': False,
        #     'ignoredepositcatchdata': False
        # }
        # response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params, headers={'Content-Type': 'application/json'})
        # if response.status_code > 200:
        #     if response.status_code == 401:  # unauthorised needs to refresh token
        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #         if success:
        #             params = {'access_token': wms_access_token}
        #             response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params,
        #                                         headers={'Content-Type': 'application/json'})
        #             if response.status_code > 200:
        #                 raise osv.except_osv(_(response.reason), _(response.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #     else:
        #         raise osv.except_osv(_(response.reason), _(response.content))
        # print "\n\nORDERSHIPPED : response.text :\n", response.text
        # for export in json.loads(response.text):
        #     picking_ids = []
        #     key1 = export.get('key1')
        #     if key1:
        #         all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'out')])
        #         if all_picking_ids:
        #             for picking in self.browse(cr, uid, all_picking_ids):
        #                 if picking.name.split('/')[1] == key1:
        #                     picking_ids = [picking.id]
        #                     break
        #     if picking_ids:
        #         picking = self.browse(cr, uid, picking_ids[0])
        #         if picking.state == 'done':
        #             continue
        #         partial_datas = {'delivery_date': fields.datetime.now()}
        #         msg = json.loads(export.get('jsonMessage'))
        #         print "\n\n\nmsg :\n", msg
        #         shipment_order_detail = msg['ShipmentConfirmation']['ShipmentOrderHeader'].get('ShipmentOrderDetail')
        #         if isinstance(shipment_order_detail, dict):
        #             shipment_order_details = [shipment_order_detail]
        #         elif isinstance(shipment_order_detail, list):
        #             shipment_order_details = shipment_order_detail
        #
        #         for shipment_order in shipment_order_details:
        #             move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(shipment_order.get('ExternLineNo'))),
        #                                                                    ('picking_id', '=', picking_ids[0])])
        #             if not move_id:
        #                 move_id = self.pool.get('stock.move').search(cr, uid, [('product_id.default_code', '=', shipment_order.get('Sku')),
        #                                                                        ('picking_id', '=', picking_ids[0])])
        #             if move_id:
        #                 move = self.pool.get('stock.move').browse(cr, uid, move_id[0])
        #                 serial_number = None
        #                 if shipment_order.get('Lottable03'):  # Select or generate serial number if required
        #                     lot_obj = self.pool.get('stock.production.lot')
        #                     serial_number_ids = lot_obj.search(cr, uid, [('name', '=', shipment_order.get('Lottable03')),
        #                                                                  ('product_id', '=', move.product_id.id)])
        #                     if serial_number_ids:
        #                         serial_number = serial_number_ids[0]
        #                     else:
        #                         serial_number = lot_obj.create(cr, uid, {
        #                             'name': shipment_order.get('Lottable03'),
        #                             'product_id': move.product_id.id,
        #                             'date': fields.datetime.now(),
        #                         })
        #
        #                 partial_datas['move%s' % move.id] = {
        #                     'product_id': move.product_id.id,
        #                     'product_qty': float(shipment_order.get('ShippedQty', 0)),
        #                     'product_uom': move.product_uom.id,
        #                     'prodlot_id': serial_number,
        #                 }
        #         if partial_datas:
        #             print "\n\npartial_datas :\n", partial_datas
        #             # self.write(cr, uid, picking_ids, {'date_done': fields.datetime.now()})
        #             # self.do_partial(cr, uid, picking_ids, partial_datas, context)

        # # PARTIALSHIPMENT
        # ordershipped_dict = {
        #     'updatestatus': '9',
        #     'transmitflagtouse': 'transmitflag5',
        #     'eventcategory': 'M',
        #     'types': ['PARTIALSHIPMENT'],
        #     'restrictrowsto': 15,
        #     'asJson': True,
        #     'generatemessages': True,
        #     'rebuildmessages': False,
        #     'ignoredepositcatchdata': False
        # }
        # response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params, headers={'Content-Type': 'application/json'})
        # if response.status_code > 200:
        #     if response.status_code == 401:  # unauthorised needs to refresh token
        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #         if success:
        #             params = {'access_token': wms_access_token}
        #             response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params,
        #                                         headers={'Content-Type': 'application/json'})
        #             if response.status_code > 200:
        #                 raise osv.except_osv(_(response.reason), _(response.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #     else:
        #         raise osv.except_osv(_(response.reason), _(response.content))
        # print "\n\nresponse.text : ", response.text
        # for export in json.loads(response.text):
        #     picking_ids = []
        #     key1 = export.get('key1')
        #     if key1:
        #         all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'out')])
        #         if all_picking_ids:
        #             for picking in self.browse(cr, uid, all_picking_ids):
        #                 if picking.name.split('/')[1] == key1:
        #                     picking_ids = [picking.id]
        #                     break
        #     if picking_ids:
        #         picking = self.browse(cr, uid, picking_ids[0])
        #         if picking.state == 'done':
        #             continue
        #         partial_datas = {'delivery_date': fields.datetime.now()}
        #         msg = json.loads(export.get('jsonMessage'))
        #         print "\n\n\nmsg :\n", msg
        #         order_detail = msg['PartialShipment']['Details'].get('OrderDetail')
        #         if isinstance(order_detail, dict):
        #             order_details = [order_detail]
        #         elif isinstance(order_detail, list):
        #             order_details = order_detail
        #
        #         for shipment_order in order_details:
        #             move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(shipment_order.get('ExternLineNo'))),
        #                                                                    ('picking_id', '=', picking_ids[0])])
        #             if not move_id:
        #                 move_id = self.pool.get('stock.move').search(cr, uid, [('product_id.default_code', '=', shipment_order.get('Sku')),
        #                                                                        ('picking_id', '=', picking_ids[0])])
        #             if move_id:
        #                 move = self.pool.get('stock.move').browse(cr, uid, move_id[0])
        #                 serial_number = None
        #                 if shipment_order.get('Lottable03'):  # Select or generate serial number if required
        #                     lot_obj = self.pool.get('stock.production.lot')
        #                     serial_number_ids = lot_obj.search(cr, uid, [('name', '=', shipment_order.get('Lottable03')),
        #                                                                  ('product_id', '=', move.product_id.id)])
        #                     if serial_number_ids:
        #                         serial_number = serial_number_ids[0]
        #                     else:
        #                         serial_number = lot_obj.create(cr, uid, {
        #                             'name': shipment_order.get('Lottable03'),
        #                             'product_id': move.product_id.id,
        #                             'date': fields.datetime.now(),
        #                         })
        #
        #                 partial_datas['move%s' % move.id] = {
        #                     'product_id': move.product_id.id,
        #                     'product_qty': float(shipment_order.get('ShippedQty', 0)),
        #                     'product_uom': move.product_uom.id,
        #                     'prodlot_id': serial_number,
        #                 }
        #         if partial_datas:
        #             print "\n\npartial_datas :\n", partial_datas
        #             self.write(cr, uid, picking_ids, {'date_done': fields.datetime.now()})
        #             self.do_partial(cr, uid, picking_ids, partial_datas, context)

        # # DELIVERY ORDER
        # ordershipped_dict = {
        #     'updatestatus': '9',
        #     'transmitflagtouse': 'transmitflag5',
        #     'eventcategory': 'M',
        #     'types': ['ORDERSHIPPED'],
        #     'restrictrowsto': 15,
        #     'asJson': True,
        #     'generatemessages': True,
        #     'rebuildmessages': False,
        #     'ignoredepositcatchdata': False
        # }
        # response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params, headers={'Content-Type': 'application/json'})
        #
        # if response.status_code > 200:
        #     if response.status_code == 401:  # unauthorised needs to refresh token
        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #         if success:
        #             params = {'access_token': wms_access_token}
        #             response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params,
        #                                         headers={'Content-Type': 'application/json'})
        #             if response.status_code > 200:
        #                 raise osv.except_osv(_(response.reason), _(response.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #     else:
        #         raise osv.except_osv(_(response.reason), _(response.content))
        #
        # for export in json.loads(response.text):
        #     picking_ids = []
        #     key1 = export.get('key1')  # "12068"
        #     if key1:
        #         all_picking_ids = self.search(cr, uid, [('name', 'like', key1), ('type', '=', 'out')])
        #         if all_picking_ids:
        #             for picking in self.browse(cr, uid, all_picking_ids):
        #                 if picking.name.split('/')[1] == key1:
        #                     picking_ids = [picking.id]
        #                     break
        #     if picking_ids:
        #         picking = self.browse(cr, uid, picking_ids[0])
        #         if picking.state == 'done':
        #             continue
        #         partial_datas = {'delivery_date': fields.datetime.now()}
        #         msg = json.loads(export.get('jsonMessage'))
        #         print "\n\n\nmsg :\n", msg
        #         # extern_receipt_key = msg['AsnClosed']['AsnHeader']['ExternReceiptKey']
        #         # receipt_key = msg['AsnClosed']['AsnHeader']['ReceiptKey']
        #         shipment_order_detail = msg['ShipmentConfirmation']['ShipmentOrderHeader'].get('ShipmentOrderDetail')
        #         if isinstance(shipment_order_detail, dict):
        #             shipment_order_details = [shipment_order_detail]
        #         elif isinstance(shipment_order_detail, list):
        #             shipment_order_details = shipment_order_detail
        #
        #         for shipment_order in shipment_order_details:
        #             move_id = self.pool.get('stock.move').search(cr, uid, [('id', '=', int(shipment_order.get('ExternLineNo'))),
        #                                                                    ('picking_id', '=', picking_ids[0])])
        #             if not move_id:
        #                 move_id = self.pool.get('stock.move').search(cr, uid, [('product_id.default_code', '=', shipment_order.get('Sku')),
        #                                                                        ('picking_id', '=', picking_ids[0])])
        #             if move_id:
        #                 move = self.pool.get('stock.move').browse(cr, uid, move_id[0])
        #                 serial_number = None
        #                 if shipment_order.get('Lot'):  # Select or generate serial number if required
        #                     lot_obj = self.pool.get('stock.production.lot')
        #                     serial_number_ids = lot_obj.search(cr, uid, [('name', '=', shipment_order.get('Lot')),
        #                                                                  ('product_id', '=', move.product_id.id)])
        #                     if serial_number_ids:
        #                         serial_number = serial_number_ids[0]
        #                     else:
        #                         serial_number = lot_obj.create(cr, uid, {
        #                             'name': shipment_order.get('Lot'),
        #                             'product_id': move.product_id.id,
        #                             'date': fields.datetime.now(),
        #                         })
        #
        #                 partial_datas['move%s' % move.id] = {
        #                     'product_id': move.product_id.id,
        #                     'product_qty': float(shipment_order.get('OriginalQty', 0)),
        #                     'product_uom': move.product_uom.id,
        #                     'prodlot_id': serial_number,
        #                 }
        #         if partial_datas:
        #             print "\n\npartial_datas :\n", partial_datas
        #             self.write(cr, uid, picking_ids, {'date_done': fields.datetime.now()})
        #             self.do_partial(cr, uid, picking_ids, partial_datas, context)

        # # INTRENAL TRANSFER
        # ordershipped_dict = {
        #     'updatestatus': '9',
        #     'transmitflagtouse': 'transmitflag5',
        #     'eventcategory': 'M',
        #     'types': ['TRANSFERFINALIZED'],
        #     'restrictrowsto': 15,
        #     'asJson': True,
        #     'generatemessages': True,
        #     'rebuildmessages': False,
        #     'ignoredepositcatchdata': False
        # }
        # response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params, headers={'Content-Type': 'application/json'})
        #
        # if response.status_code > 200:
        #     if response.status_code == 401:  # unauthorised needs to refresh token
        #         success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #         if success:
        #             params = {'access_token': wms_access_token}
        #             response = requests.request("POST", url, data=json.dumps(ordershipped_dict), params=params,
        #                                         headers={'Content-Type': 'application/json'})
        #             if response.status_code > 200:
        #                 raise osv.except_osv(_(response.reason), _(response.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #     else:
        #         raise osv.except_osv(_(response.reason), _(response.content))
        #
        # print "\n\n\n\nLog Text response.text:\n\n", response.text
        # for export in json.loads(response.text):
        #     msg = json.loads(export.get('jsonMessage'))
        #     print "\n\n\nexport msg :\n", msg
        #     transfer_key = msg['TransferConfirmation']['TransferConfirmationHeader'].get('TransferKey')
        #     if transfer_key and not self.pool.get('stock.picking').search(cr, uid, [('wms_api_order_id', '=', transfer_key)]):
        #         # Fetch internal transfer details from WMS
        #         transfer_url = wms_api_endpoint + '/' + wms_warehouse_name + '/transfers/' + transfer_key
        #         transfer_resp = requests.request("GET", transfer_url, data={}, params={'access_token': wms_access_token},
        #                                          headers={'Content-Type': 'application/json'})
        #
        #         if transfer_resp.status_code > 200:
        #             if transfer_resp.status_code == 401:  # unauthorised needs to refresh token
        #                 success, wms_access_token = self.pool.get('stock.picking').refresh_wms_token(cr, uid)
        #                 if success:
        #                     transfer_resp = requests.request("GET", transfer_url, data={}, params={'access_token': wms_access_token},
        #                                                      headers={'Content-Type': 'application/json'})
        #                     if transfer_resp.status_code > 200:
        #                         raise osv.except_osv(_(transfer_resp.reason), _(transfer_resp.content))
        #         else:
        #                 raise osv.except_osv(_("Problem In Refresh Token"), wms_access_token or 'Refresh token is expired')
        #             else:
        #                 raise osv.except_osv(_(transfer_resp.reason), _(transfer_resp.content))
        #
        #         if transfer_resp.status_code == 200:
        #             picking_obj = self.pool.get('stock.picking')
        #             move_obj = self.pool.get('stock.move')
        #             print "\n\n\n\ntransfer_resp resp:\n\n", transfer_resp.text
        #             transfer = json.loads(transfer_resp.text)
        #             wf_service = netsvc.LocalService("workflow")
        #             wms_order_id = transfer.get('transferkey', '')
        #
        #             # Create Internal Moves first and add all the move lines to it
        #             move_lines = []
        #             for transfer_details in transfer.get('transferdetails'):
        #                 print "transfer_details : ", transfer_details
        #                 sku = transfer_details.get('fromsku')
        #                 if sku:
        #                     product_ids = self.pool.get('product.product').search(cr, uid, [('default_code', '=ilike', sku)])
        #                 dest_location = transfer_details.get('toloc')
        #                 if dest_location:
        #                     dest_location_ids = self.pool.get('stock.location').search(cr, uid, [('name', '=ilike', dest_location)])
        #                 src_location = transfer_details.get('fromloc')
        #                 if src_location:
        #                     src_location_ids = self.pool.get('stock.location').search(cr, uid, [('name', '=ilike', src_location)])
        #
        #                 if product_ids and dest_location_ids and src_location_ids:
        #                     prod = self.pool.get('product.product').browse(cr, uid, product_ids[0])
        #                     name = self.pool.get('product.product').name_get(cr, uid, product_ids[0], context=context)[0][1]
        #                     # adddate=transfer_details.get('adddate','')
        #                     # if adddate:
        #                     #     adddate = adddate.replace('T',' ')
        #                     #     adddate = adddate[:19]
        #                     effectivedate = transfer_details.get('effectivedate', '')
        #                     if effectivedate:
        #                         effectivedate = effectivedate.replace('T', ' ')
        #                         effectivedate = effectivedate[:19]
        #
        #                     qty = transfer_details.get('toqty', 0)
        #                     serial_number = transfer_details.get('lottable03')
        #                     if serial_number:
        #                         serial_number = self.pool.get('stock.production.lot').search(cr, uid, [('name', '=', serial_number),
        #                                                                                                ('product_id', '=', product_ids[0])])
        #                         if serial_number:
        #                             serial_number = serial_number[0]
        #                             # # Check Quantity of that serial number is present or not, if not then set none
        #                             # lot_reply = move_obj.onchange_lot_id(cr, uid, [], prodlot_id=serial_number, product_qty=qty,
        #                             #                                      loc_id=src_location_ids[0], product_id=product_ids[0],
        #                             #                                      uom_id=prod.uom_id.id)
        #                             # if lot_reply:
        #                             #     _logger.info(str(lot_reply['title']) + ' : ' + str(lot_reply['message']))
        #                             #     serial_number = None
        #                         else:
        #                             serial_number = None
        #                     move_dict = {
        #                         'name': name,
        #                         'product_id': product_ids[0],
        #                         'product_qty': qty,
        #                         'product_uos_qty': qty,
        #                         'product_uom': prod.uom_id.id,
        #                         'product_uos': prod.uom_id.id,
        #                         'location_id': src_location_ids[0],
        #                         'location_dest_id': dest_location_ids[0],
        #                         'wms_order_id': wms_order_id,
        #                         'prodlot_id': serial_number,
        #                         'origin': 'WMS IN IMPORT API ' + wms_order_id,
        #                         'type': 'internal',
        #                         'date_expected': effectivedate,
        #                     }
        #                     move_lines.append((0, 0, move_dict))
        #             print "\n\nmove_lines :\n", move_lines
        #             if move_lines:
        #                 new_picking_id = picking_obj.create(cr, uid, {
        #                     'type': 'internal',
        #                     'origin': wms_order_id,
        #                     'move_lines': move_lines,
        #                     'wms_api_order_id': wms_order_id,
        #                     'note': 'Created form WMS API Import Schedular Import WMS ID : ' + str(wms_order_id),
        #                 }, context=context)
        #                 new_picking = picking_obj.browse(cr, uid, new_picking_id, context=context)
        #                 print "new_picking : ", new_picking
        #                 for line in new_picking.move_lines:
        #                     print "line : ", line
        #
        #                 picking_obj.draft_force_assign(cr, uid, [new_picking_id])
        #                 if new_picking.state == 'confirmed':
        #                     if picking_obj.action_assign(cr, uid, [new_picking_id]):
        #                         print "YYYYYYYYYYYYYYYYYYYYY"
        #                         picking_obj.action_process(cr, uid, [new_picking_id], context=context)
        #                         partial_datas = {'delivery_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        #                         # partial_datas = {'delivery_date': receive_date}
        #                         for move in new_picking.move_lines:
        #                             if move.state == 'assigned':
        #                                 partial_datas['move%s' % move.id] = {
        #                                     'product_id': move.product_id.id,
        #                                     'product_qty': move.product_qty,
        #                                     'product_uom': move.product_uom.id,
        #                                     'prodlot_id': move.prodlot_id.id,
        #                                 }
        #                         picking_obj.do_partial(cr, uid, [new_picking.id], partial_datas, context)
        #                     wf_service.trg_write(uid, 'stock.picking', new_picking.id, cr)

        return True


class stock_picking_in(osv.osv):
    _inherit = 'stock.picking.in'
    _columns = {
        'wms_api_order_id': fields.char('WMS API Order Id', readonly=True)
    }

    def process_in_export(self, cr, uid, ids, context=None):
        res = super(stock_picking_in, self).process_in_export(cr, uid, ids, context)
        for picking in self.browse(cr, uid, ids, context=context):
            # If any Move have colton as a Dest. location
            colton_location = False
            for move in picking.move_lines:
                if move.location_dest_id.id == 21382:  # Inland Empire Location ID:
                    colton_location = True
                    break
            if colton_location:
                self.pool.get('stock.picking').wms_api_in_export(cr, uid, picking, context=context)
        return res


class stock_picking_out(osv.osv):
    _inherit = 'stock.picking.out'
    _columns = {
        'wms_api_order_id': fields.char('WMS API Order Id', readonly=True)
    }

    def process_out_export(self, cr, uid, ids, context=None):
        res = super(stock_picking_out, self).process_out_export(cr, uid, ids, context)
        # out export only if move source location is colton, Send move To Shipworks if user click this button
        model, location = self.pool.get('ir.model.data').get_object_reference(cr, uid, 'atlas_wms_integration', 'stock_location_colton')
        inland_empire_id = 21382  # Inland Empire Location
        for picking in self.browse(cr, uid, ids, context=context):
            inland_empire = False
            # If any Move have colton as a Source location
            colton_location = False
            for move in picking.move_lines:
                if move.location_id.id == location:
                    colton_location = True
                elif move.location_id.id == inland_empire_id:
                    inland_empire = True
                    # if move.state in ['confirmed', 'assigned']:
                    #     current_utc_time = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                    #     self.pool.get('stock.move').write(cr, uid, [move.id], {'shipworks_date': current_utc_time})
            # if colton_location:
            #     return self.pool.get('stock.picking').wms_api_out_export(cr, uid, picking, context=context)
            if inland_empire:
                self.pool.get('stock.picking').wms_api_out_export(cr, uid, picking, context=context)
        return res
