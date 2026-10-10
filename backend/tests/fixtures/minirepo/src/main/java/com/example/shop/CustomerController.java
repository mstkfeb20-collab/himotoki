package com.example.shop;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;

@Controller
class CustomerController {

	private static final String FORM = "customers/customerForm";

	private final CustomerRepository customers;

	CustomerController(CustomerRepository customers) {
		this.customers = customers;
	}

	@GetMapping("/customers/new")
	public String initForm() {
		return FORM;
	}

	@PostMapping("/customers/new")
	public String save(Customer customer) {
		if (customer.getZip() == null) {
			return FORM;
		}
		this.customers.save(customer);
		return "redirect:/customers";
	}

	@GetMapping("/customers/zip/{zip}")
	public String byZip(@PathVariable String zip) {
		int pageSize = 20;
		this.customers.findByZipStartingWith(zip);
		return "customers/customerList";
	}

}
