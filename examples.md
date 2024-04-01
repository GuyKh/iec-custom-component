# Few examples of cards/dashboards using the component


### Is paid invoice (by Noam Shaharabani)
![card not paid](assets/is_paid_false.png)
![card paid](assets/is_paid_true.png)

```
type: custom:vertical-stack-in-card
cards:
- type: custom:button-card
  entity: binary_sensor.is_last_iec_invoice_paid
  show_state: false
  show_icon: false
  state:
    - value: 'off'
      name: החשבונית האחרונה עדיין לא שולמה
      styles:
        card:
          - color: white
          - background: '#FF8080'
    - value: 'on'
      name: החשבונית האחרונה שולמה
      styles:
        card:
          - color: black
          - background: '#CDFAD5'
  styles:
    card:
    - border: 0px
    - height: 0px
    - margin-top: null
    - type: horizontal-stack
  cards:
- type: horizontal-stack
  cards:
  - type: custom:button-card
    entity: sensor.last_iec_bill_electric_cost
    show_state: true
    show_name: true
    name: לתשלום
    show_icon: true
    icon: mdi:currency-ils
    styles:
      card:
        - border: 0px
        - padding-top: 15px
      name:
        - font-size: 14px
      state:
        - font-size: 15px
  - type: custom:button-card
    entity: sensor.last_iec_bill_length_in_days
    show_state: false
    show_name: true
    name: לתקופה של
    show_icon: true
    icon: mdi:sun-angle
    show_label: true
    label: |
      [[[
      return entity.state + ' ימים';
      ]]]
    styles:
      card:
        - border: 0px
        - padding-top: 15px
      name:
        - font-size: 14px
      label:
        - font-size: 15px
  - type: custom:button-card
    entity: sensor.last_iec_bill_payment_date
    show_state: true
    show_name: true
    name: לתשלום עד
    show_icon: true
    icon: mdi:calendar
    styles:
      card:
        - border: 0px
        - padding-bottom: 15px
        - padding-top: 15px
      name:
        - font-size: 14px
      state:
        - font-size: 15px
  - type: custom:button-card
    entity: sensor.last_iec_bill_electric_usage_to_date
    show_state: true
    show_name: true
    name: קילוואט
    show_icon: true
    icon: mdi:lightning-bolt
    styles:
      card:
        - border: 0px
        - padding-bottom: 15px
        - padding-top: 15px
      name:
        - font-size: 14px
      state:
        - font-size: 15px
      
```