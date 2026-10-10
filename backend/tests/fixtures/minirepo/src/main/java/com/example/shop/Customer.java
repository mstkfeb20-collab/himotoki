package com.example.shop;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.validation.constraints.Pattern;

/** A shop customer. */
@Entity
@Table(name = "customers")
public class Customer {

	@Id
	private Integer id;

	@Column(name = "full_name")
	private String name;

	@Column
	@Pattern(regexp = "\\d{7}", message = "{zip.invalid}")
	private String zip;

	public String getName() {
		return this.name;
	}

	public void setName(String name) {
		this.name = name;
	}

	public String getZip() {
		return this.zip;
	}

	public void setZip(String zip) {
		this.zip = zip;
	}

}
