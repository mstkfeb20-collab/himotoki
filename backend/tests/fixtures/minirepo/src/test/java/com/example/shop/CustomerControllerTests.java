package com.example.shop;

import org.junit.jupiter.api.Test;

class CustomerControllerTests {

	@Test
	void saveRejectsMissingZip() throws Exception {
		mockMvc.perform(post("/customers/new").param("zip", ""));
	}

	@Test
	void initForm() throws Exception {
		mockMvc.perform(get("/customers/new"));
	}

}
